#!/usr/bin/env python

# Copyright (c) 2017, DIANA-HEP
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import ast
import re
import sys
import inspect
from collections import namedtuple

import numpy
import numba
from numba.types import *

from arrowed.thirdparty.meta.decompiler.instructions import make_function
from arrowed.thirdparty.meta import dump_python_source

from arrowed.schema import *

py2 = (sys.version_info[0] <= 2)
string_types = (unicode, str) if py2 else (str, bytes)

################################################################ interface

class Compiled(object):
    def __init__(self, transformed, parameters, env, numbaargs):
        self.transformed = transformed
        self.parameters = parameters
        self.env = env
        self.numbaargs = numbaargs

        full = ast.Module([self.transformed], lineno=1, col_offset=0)

        envcopy = env.copy()
        eval(__builtins__["compile"](full, transformed.name, "exec"), envcopy)
        self.compiled = envcopy[transformed.name]

        if self.numbaargs is not None:
            self.executable = numba.jit(**numbaargs)(self.compiled)
        else:
            self.executable = self.compiled

    def __call__(self, resolved, *args):
        arguments = []
        argsi = 0
        for parameter in self.parameters.order:
            if isinstance(parameter, TransformedParameter):
                for symbol in parameter.transformed:
                    member, attr = parameter.sym2obj[symbol]
                    arguments.append(resolved.findbybase(member).get(attr))
            else:
                if argsi >= len(args):
                    raise TypeError("too few extra (non-columnar object) arguments provided")
                arguments.append(args[argsi])
                argsi += 1

        if argsi < len(args):
            raise TypeError("too many extra (non-columnar object) arguments provided")

        out = self.executable(*arguments)
        if isinstance(out, ToProxy):
            return resolved.findbybase(self.parameters.order[out.parameterid].members[out.memberid]).proxy(out.index)
        else:
            return out

def compile(function, paramtypes, env={}, numbaargs={"nopython": True, "nogil": True}, debug=False):
    # turn the 'function' argument into the syntax tree of a function
    if isinstance(function, string_types):
        sourcefile = "<string>"
        function = withequality(ast.parse(function).body[0])
        if isinstance(function, ast.Expr) and isinstance(function.value, ast.Lambda):
            if py2:
                return withequality(ast.FunctionDef("lambda", function.value.args, [function.value.body], []))
            else:
                return withequality(ast.FunctionDef("lambda", function.value.args, [function.value.body], [], None))

        if not isinstance(function, ast.FunctionDef):
            raise TypeError("string to compile must declare exactly one function")

    else:
        sourcefile = inspect.getfile(function)
        function = tofunction(function)

    builtins = __builtins__.copy()
    builtins.update(env)
    env = builtins

    # get a list of all symbols used by the function and any other functions it references
    symbolsused = set(env)
    externalfcns = {}
    def search(node):
        if isinstance(node, ast.AST):
            if isinstance(node, ast.Name):
                symbolsused.add(node.id)
            elif isinstance(node, ast.FunctionDef):
                symbolsused.add(node.name)

            if isinstance(node, ast.Call):
                try:
                    obj = eval(__builtins__["compile"](ast.Expression(node.func), "", "eval"), env)
                except Exception as err:
                    raise err.__class__("code to compile calls the expression below, but it is not defined in the environment (env):\n\n    {0}".format(dump_python_source(node.func).strip()))
                else:
                    if hasattr(obj, "__code__"):
                        externalfcns[node.func] = tofunction(obj)
                        search(externalfcns[node.func])

            for x in node._fields:
                search(getattr(node, x))

        elif isinstance(node, list):
            for x in node:
                search(x)
                
    search(function)

    # symbol name generator
    def sym(key):
        if key not in sym.names:
            prefix = sym.bad.sub("", key)
            if len(prefix) == 0 or prefix[0] in sym.numberchars:
                prefix = "_" + prefix

            trial = prefix
            number = 2
            while trial in symbolsused:
                trial = "{0}_{1}".format(prefix, number)
                number += 1

            symbolsused.add(trial)
            sym.names[key] = trial
            if key != trial:
                sym.remapped.append((key, trial))

        return sym.names[key]

    sym.bad = re.compile(r"[^a-zA-Z0-9_]*")
    sym.numberchars = [chr(x) for x in range(ord("0"), ord("9") + 1)]
    sym.names = {}
    sym.remapped = []

    env[sym("passnone")] = passnone
    env[sym("listget")] = listget
    env[sym("listlen")] = listlen
    env[sym("ToProxy")] = ToProxy

    # do the code transformation
    transformed, parameters = transform(function, paramtypes, externalfcns, env, sym, sourcefile)

    if debug:
        try:
            before = dump_python_source(function).strip()
        except Exception:
            before = ast.dump(function)
        try:
            after = dump_python_source(transformed).strip()
        except Exception:
            after = ast.dump(transformed)
        print("")
        print("Before transformation:\n----------------------\n{0}\n\nAfter transformation:\n---------------------\n{1}".format(before, after))
        if len(sym.remapped) > 0:
            print("\nRemapped symbol names:\n----------------------")
            formatter = "    {0:%ds} --> {1}" % max([len(name) for name, value in sym.remapped] + [0])
            for name, value in sym.remapped:
                print(formatter.format(name, value))
        ### FIXME: projections are not correct
        # print("\nProjections:\n------------")
        # for parameter in parameters.order:
        #     print("    {0}: {1}".format(parameter.index, parameter.originalname))
        #     if isinstance(parameter, TransformedParameter):
        #         projection = parameter.projection()
        #         if projection is not None:
        #             print(projection.format("         "))
        print("")

    return Compiled(transformed, parameters, env, numbaargs)

################################################################ functions inserted into code

def newrefusenone(env, sym, what, lineno, sourcefile):
    message = "None found where {0} required\n\nat line {lineno} of {sourcefile}".format(
        what, lineno=lineno, sourcefile=sourcefile)

    @numba.njit(int32(numba.optional(int32)))
    def refusenone(index):
        if index is None:
            raise TypeError(message)
        return index

    symbol = sym("refusenone")
    env[symbol] = refusenone
    return symbol

@numba.njit
def passnone(array, index):
    if index is None:
        return None
    else:
        return array[index]

@numba.njit(int32(int32[:], int32[:], int32, int32))
def listget(begin, end, outerindex, index):
    offset = begin[outerindex]
    size = end[outerindex] - offset
    if index < 0:
        index = size + index
    if index < 0 or index >= size:
        raise IndexError("index out of range")
    return offset + index

@numba.njit(int32(int32[:], int32[:], int32))
def listlen(begin, end, index):
    return end[index] - begin[index]

ToProxy = namedtuple("ToProxy", ["parameterid", "memberid", "index"])

################################################################ for generating ASTs

# mix-in for defining equality on ASTs
class WithEquality(object):
    def __eq__(self, other):
        if isinstance(other, ast.AST):
            assert isinstance(other, WithEquality)
        return self.__class__ == other.__class__ and all(getattr(self, x) == getattr(other, x) for x in self._fields)

    def __hash__(self):
        hashable = lambda x: tuple(x) if isinstance(x, list) else x
        return hash((self.__class__, tuple(hashable(getattr(self, x)) for x in self._fields)))

def withequality(pyast):
    if isinstance(pyast, ast.AST):
        if not isinstance(pyast, WithEquality):
            if pyast.__class__.__name__ not in withequality.classes:
                withequality.classes[pyast.__class__.__name__] = type(pyast.__class__.__name__, (pyast.__class__, WithEquality), {})

            out = withequality.classes[pyast.__class__.__name__](*[withequality(getattr(pyast, x)) for x in pyast._fields])
            out.lineno = getattr(pyast, "lineno", 1)
            out.col_offset = getattr(pyast, "col_offset", 0)
            out.atype = getattr(pyast, "atype", untracked)
            return out

        else:
            return pyast

    elif isinstance(pyast, list):
        return [withequality(x) for x in pyast]

    else:
        return pyast

withequality.classes = {}
    
def compose(pyast, **replacements):
    def recurse(x):
        if isinstance(x, ast.AST):
            if isinstance(x, ast.Name) and x.id in replacements:
                x = replacements[x.id]

            if isinstance(x, ast.Attribute) and x.attr in replacements:
                x.attr = replacements[x.attr]

            if isinstance(x, ast.FunctionDef) and x.name in replacements:
                x.name = replacements[x.name]

            for f in x._fields:
                setattr(x, f, recurse(getattr(x, f)))

            return x

        elif isinstance(x, list):
            return [recurse(xi) for xi in x]

        else:
            return x

    return recurse(pyast)

def setlinenoatype(node, lineno, atype):
    if lineno is None:
        node.lineno, node.col_offset = 1, 0
    else:
        node.lineno, node.col_offset = lineno.lineno, lineno.col_offset
    node.atype = atype
    return node

def retyped(pyast, atype):
    assert isinstance(pyast, WithEquality)
    return setlinenoatype(pyast.__class__(*[getattr(pyast, x) for x in pyast._fields]), pyast, atype)

def rebuilt(original, *args):
    return setlinenoatype(original.__class__(*args), original, original.atype)

def toexpr(string, lineno=None, atype=None, **replacements):
    return setlinenoatype(compose(withequality(ast.parse(string).body[0].value), **replacements), lineno=lineno, atype=atype)

def tostmt(string, lineno=None, atype=None, **replacements):
    return setlinenoatype(compose(withequality(ast.parse(string).body[0]), **replacements), lineno=lineno, atype=atype)

def tostmts(string, lineno=None, atype=None, **replacements):
    return setlinenoatype(compose(withequality(ast.parse(string).body), **replacements), lineno=lineno, atype=atype)

def toname(string, lineno=None, atype=None, ctx=ast.Load()):
    return setlinenoatype(withequality(ast.Name(string, ctx)), lineno=lineno, atype=atype)

def toliteral(obj, lineno=None, atype=None):
    if isinstance(obj, str):
        return setlinenoatype(withequality(ast.Str(obj)), lineno=lineno, atype=atype)
    elif isinstance(obj, (int, float)):
        return setlinenoatype(withequality(ast.Num(obj)), lineno=lineno, atype=atype)
    else:
        raise AssertionError

def tofunction(obj):
    if not hasattr(obj, "__code__"):
        raise TypeError("attempting to compile {0}, but it is not a Python function (something with a __code__ attribute)".format(repr(obj)))
    out = make_function(obj.__code__)
    if isinstance(out, ast.Lambda):
        if py2:
            return withequality(ast.FunctionDef("lambda", out.args, [out.body], []))
        else:
            return withequality(ast.FunctionDef("lambda", out.args, [out.body], [], None))
    else:
        return withequality(out)

################################################################ the main transformation function

class Possibility(object):
    def __init__(self, schema, condition=None):
        self.schema = schema
        self.condition = condition

class ArrowedType(object):
    def __init__(self, possibilities, parameter):
        if not isinstance(possibilities, (list, tuple)):
            possibilities = [possibilities]
        possibilities = [x if isinstance(x, Possibility) else Possibility(x) for x in possibilities]
        self.possibilities = possibilities
        self.parameter = parameter
        self.isparameter = False
        self.nullable = False

    def setnullable(self, value=True):
        out = ArrowedType(self.possibilities, self.parameter)
        out.isparameter = self.isparameter
        out.nullable = value
        return out

    def generate(self, handler):
        out = None
        for possibility in reversed(self.possibilities):
            result = handler(possibility.schema)
            if possibility.condition is None:
                assert out is None
                out = result
            else:
                assert out is not None
                out = toexpr("CONSEQUENT if PREDICATE else ALTERNATE",
                             CONSEQUENT = result,
                             PREDICATE = possibility.condition,
                             ALTERNATE = out,
                             lineno = result,
                             atype = result.atype)
        return out

    def __eq__(self, other):
        return isinstance(other, ArrowedType) and self.parameter is other.parameter and len(self.possibilities) == len(other.possibilities) and all(self.parameter.reverse_members[id(x.schema)] == other.parameter.reverse_members[id(y.schema)] for x, y in zip(self.possibilities, other.possibilities))

    def __ne__(self, other):
        return not self.__eq__(other)

untracked = ArrowedType([], None)
nullable = ArrowedType([], None)

class Parameter(object):
    def __init__(self, index, originalname, default):
        self.index = index
        self.originalname = originalname
        self.default = default
        self.atype = untracked

    def args(self):
        if py2:
            return [ast.Name(self.originalname, ast.Param())]
        else:
            return [ast.arg(self.originalname, None)]

    def defaults(self):
        if self.default is None:
            return []
        else:
            return [self.default]

class TransformedParameter(Parameter):
    def __init__(self, index, originalname, atype):
        self.index = index
        self.originalname = originalname
        self.atype = atype
        self.atype.parameter = self
        self.atype.isparameter = True
        self.transformed = []

        assert len(self.atype.possibilities) == 1
        self.schema = self.atype.possibilities[0].schema

        self.members = self.schema.members()
        self.reverse_members = dict((id(m), i) for i, m in enumerate(self.members))
        assert len(self.members) == len(self.reverse_members)

        self.required = [False] * len(self.members)
        self.sym2obj = {}

    def require(self, member, attr, sym):
        memberid = self.reverse_members[id(member)]
        key = "par{0}_mem{1}_{2}_{3}".format(self.index, memberid, member.name, attr)
        symbol = sym(key)
        if symbol not in self.transformed:
            self.transformed.append(symbol)
        self.sym2obj[symbol] = (member, attr)
        self.required[memberid] = True
        return symbol

    def required_members(self):
        return [m for m, r in zip(self.members, self.required) if r]

    ### FIXME: projections are not correct
    # def projection(self):
    #     return self.schema.projection(self.required_members())

    def args(self):
        if py2:
            return [ast.Name(x, ast.Param()) for x in self.transformed]
        else:
            return [ast.arg(x, None) for x in self.transformed]

    def defaults(self):
        return []

class Parameters(object):
    def __init__(self, order):
        self.order = order
        self.lookup = dict((x.originalname, x) for x in self.order)

    def istransformed(self, name):
        return isinstance(self.lookup.get(name, None), TransformedParameter)

    @property
    def transformed(self):
        return [x for x in self.order if isinstance(x, TransformedParameter)]

    def atype(self, name):
        if name in self.lookup:
            return self.lookup[name].atype
        else:
            return untracked

    def args(self):
        if py2:
            return withequality(ast.arguments(sum((x.args() for x in self.order), []), None, None, sum((x.defaults() for x in self.order), [])))
        else:
            return withequality(ast.arguments(sum((x.args() for x in self.order), []), None, [], [], None, sum((x.defaults() for x in self.order), [])))

def transform(function, paramtypes, externalfcns, env, sym, sourcefile):
    # check for too much dynamism
    if function.args.vararg is not None:
        raise TypeError("function {0} has *args, which are not allowed in compiled functions".format(repr(function.name)))
    if function.args.kwarg is not None:
        raise TypeError("function {0} has **kwds, which are not allowed in compiled functions".format(repr(function.name)))

    # identify which parameters will be transformed (probably from a single parameter to multiple)
    defaults = [None] * (len(function.args.args) - len(function.args.defaults)) + function.args.defaults
    parameters = []
    symtable = {}
    for index, (param, default) in enumerate(zip(function.args.args, defaults)):
        if py2:
            assert isinstance(param, ast.Name) and isinstance(param.ctx, ast.Param)
            paramname = param.id
        else:
            assert isinstance(param, ast.arg)
            paramname = param.arg

        if index in paramtypes and paramname in paramtypes:
            raise ValueError("parameter at index {0} and parameter named {1} are the same parameter in paramtypes".format(index, repr(paramname)))

        if index in paramtypes:
            paramtype = paramtypes[index]
        elif paramname in paramtypes:
            paramtype = paramtypes[paramname]
        else:
            paramtype = None

        if paramtype is None:
            parameters.append(Parameter(index, paramname, default))
            symtable[paramname] = untracked
        else:
            if default is not None:
                raise ValueError("parameter {0} is an argument defined in paramtypes, which is not allowed to have default parameters")
            paramtype = ArrowedType(paramtype, None)  # modified by TransformedParameter constructor
            parameters.append(TransformedParameter(index, paramname, paramtype))
            symtable[paramname] = paramtype

    everything = globals()
    def recurse(pyast):
        if isinstance(pyast, ast.AST):
            handlername = "do_" + pyast.__class__.__name__
            if handlername in everything:
                return everything[handlername](pyast, symtable, externalfcns, env, sym, sourcefile, recurse)
            else:
                out = pyast.__class__(*[recurse(getattr(pyast, x)) for x in pyast._fields])
                out.lineno = pyast.lineno
                out.col_offset = pyast.col_offset
                out.atype = pyast.atype
                return out

        elif isinstance(pyast, list):
            return [recurse(x) for x in pyast]

        else:
            return pyast

    transformed = recurse(function)
    parametersobj = Parameters(parameters)
    transformed.args = parametersobj.args()

    return transformed, parametersobj

################################################################ implicit conversion rules

def implicit(node, sym):
    def handler(schema):
        if isinstance(schema, Primitive):
            array = node.atype.parameter.require(schema, "array", sym)

            if node.atype.nullable or schema.nullable:
                return toexpr("PASSNONE(ARRAY, INDEX)",
                              PASSNONE = toname(sym("passnone")),
                              ARRAY = toname(array),
                              INDEX = node,
                              lineno = node,
                              atype = untracked)
            else:
                return toexpr("ARRAY[INDEX]",
                              ARRAY = toname(array),
                              INDEX = node,
                              lineno = node,
                              atype = untracked)

        # TODO: handle pointers and such

        else:
            return node

    if node.atype is untracked:
        return node
    elif node.atype is nullable:
        return node
    else:
        return node.atype.generate(handler)

################################################################ specialized rules for each Python AST type

# Add ()

# alias ("name", "asname")

# And ()

# arg ("arg", "annotation") # Py3 only

# arguments ("args", "vararg", "kwarg", "defaults")                               # Py2
# arguments ("args", "vararg", "kwonlyargs", "kw_defaults", "kwarg", "defaults")  # Py3

# Assert ("test", "msg")

# Assign ("targets", "value")
def do_Assign(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    value = recurse(node.value)

    def assign(lhs, rhs):
        if isinstance(lhs, ast.Name):
            if rhs.atype is untracked:
                pass
            elif rhs.atype is nullable:
                if lhs.id in symtable:
                    symtable[lhs.id] = symtable[lhs.id].setnullable()
                else:
                    symtable[lhs.id] = nullable
            else:
                if lhs.id in symtable and symtable[lhs.id] is nullable:
                    symtable[lhs.id] = rhs.atype.setnullable()
                elif lhs.id in symtable and symtable[lhs.id] == rhs.atype:
                    pass
                elif lhs.id in symtable:
                    raise TypeError("cannot use the same variable ({0}) for different parts of a dataset structure\n\nat line {lineno} of {sourcefile}".format(
                        lhs.id, lineno=node.lineno, sourcefile=sourcefile))
                else:
                    symtable[lhs.id] = rhs.atype
                    
        elif isinstance(lhs, (ast.List, ast.Tuple)):
            if rhs.atype is not untracked:
                raise NotImplementedError  # unpack Lists and Tuples
            elif isinstance(rhs, ast.Tuple):
                for lelt, relt in zip(lhs.elts, rhs.elts):
                    assign(lelt, relt)

    for target in node.targets:
        assign(target, value)

    return rebuilt(node, node.targets, value)

# Attribute ("value", "attr", "ctx")
def do_Attribute(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    node = rebuilt(node, recurse(node.value), node.attr, node.ctx)

    if node.value.atype is untracked:
        return node

    else:
        def handler(schema):
            if isinstance(schema, Record):
                if node.attr in schema.contents:
                    return retyped(node.value, ArrowedType(schema.contents[node.attr], node.value.atype.parameter))
                elif schema.name is None:
                    raise AttributeError("attribute {0} not found in record with structure:\n\n{1}\n\nat line {lineno} of {sourcefile}".format(
                        repr(node.attr), schema.format("    "), lineno=node.lineno, sourcefile=sourcefile))
                else:
                    raise AttributeError("attribute {0} not found in record {1}\n\nat line {lineno} of {sourcefile}".format(
                        repr(node.attr), schema.name, lineno=node.lineno, sourcefile=sourcefile))
            else:
                raise TypeError("object is not a record:\n\n{0}\n\nat line {lineno} of {sourcefile}".format(
                    schema.format("    "), lineno=node.lineno, sourcefile=sourcefile))

        return implicit(node.value.atype.generate(handler), sym)

# AugAssign ("target", "op", "value")

# AugLoad ()

# AugStore ()

# BinOp ("left", "op", "right")

# BitAnd ()

# BitOr ()

# BitXor ()

# BoolOp ("op", "values")

# Break ()

# Bytes ("s",)  # Py3 only

# Call ("func", "args", "keywords", "starargs", "kwargs")
def do_Call(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    func = recurse(node.func)
    args = recurse(node.args)
    keywords = recurse(node.keywords)
    starargs = recurse(node.starargs)
    kwargs = recurse(node.kwargs)

    if isinstance(func, ast.Name) and func.id in env and env[func.id] is len:
        if len(args) != 1:
            raise TypeError("len() takes exactly one argument ({0} given)\n\nat line {lineno} of {sourcefile}".format(
                len(args), lineno=node.lineno, sourcefile=sourcefile))

        def handler(schema):
            if isinstance(schema, List):
                beginarray = args[0].atype.parameter.require(schema, "beginarray", sym)
                endarray = args[0].atype.parameter.require(schema, "endarray", sym)

                if args[0].atype.nullable or schema.nullable:
                    index = toexpr("REFUSENONE(INDEX)",
                                   REFUSENONE = toname(newrefusenone(env, sym, "list", node.value.lineno, sourcefile)),
                                   INDEX = args[0])
                else:
                    index = args[0]

                return toexpr("LISTLEN(BEGIN, END, INDEX)",
                              LISTLEN = toname(sym("listlen")),
                              BEGIN = toname(beginarray),
                              END = toname(endarray),
                              INDEX = index,
                              lineno = node,
                              atype = untracked)

            else:
                raise TypeError("object is not a list:\n\n{0}\n\nat line {lineno} of {sourcefile}".format(
                    schema.format("    "), lineno=node.lineno, sourcefile=sourcefile))

        if args[0].atype is untracked or args[0].atype is nullable:
            return rebuilt(node, func, args, keywords, starargs, kwargs)
        else:
            return args[0].atype.generate(handler)

    else:
        return rebuilt(node, func, args, keywords, starargs, kwargs)

# ClassDef ("name", "bases", "body", "decorator_list")                                   # Py2
# ClassDef ("name", "bases", "keywords", "starargs", "kwargs", "body", "decorator_list") # Py3

# Compare ("left", "ops", "comparators")

# comprehension ("target", "iter", "ifs")

# Continue ()

# Del ()

# Delete ("targets",)

# DictComp ("key", "value", "generators")

# Dict ("keys", "values")

# Div ()

# Ellipsis ()

# Eq ()

# ExceptHandler ("type", "name", "body")

# Exec ("body", "globals", "locals") # Py2 only

# Expression ("body",)

# Expr ("value",)

# ExtSlice ("dims",)

# FloorDiv ()

# For ("target", "iter", "body", "orelse")
def do_For(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    node = rebuilt(node, node.target, recurse(node.iter), node.body, recurse(node.orelse))

    if node.iter.atype is untracked:
        node.body = recurse(node.body)
        return node

    else:
        if not isinstance(node.target, ast.Name):
            raise NotImplementedError

        def handler(schema):
            if isinstance(schema, List):
                beginarray = node.iter.atype.parameter.require(schema, "beginarray", sym)
                endarray = node.iter.atype.parameter.require(schema, "endarray", sym)

                symtable[node.target.id] = ArrowedType(schema.contents, node.iter.atype.parameter)

                return toexpr("range(BEGIN[OUTERINDEX], END[OUTERINDEX])",
                              BEGIN = toname(beginarray),
                              END = toname(endarray),
                              OUTERINDEX = node.iter,
                              lineno = node)

            else:
                raise IndexError("object is not a list:\n\n{0}\n\nat line {lineno} of {sourcefile}".format(
                    schema.format("    "), lineno=node.lineno, sourcefile=sourcefile))

        node.iter = node.iter.atype.generate(handler)
        node.body = recurse(node.body)
        return node

# FunctionDef ("name", "args", "body", "decorator_list")             # Py2
# FunctionDef ("name", "args", "body", "decorator_list", "returns")  # Py3

# GeneratorExp ("elt", "generators")

# Global ("names",)

# Gt ()

# GtE ()

# IfExp ("test", "body", "orelse")

# If ("test", "body", "orelse")

# ImportFrom ("module", "names", "level")

# Import ("names",)

# In ()

# Index ("value",)

# Interactive ("body",)

# Invert ()

# Is ()

# IsNot ()

# keyword ("arg", "value")

# Lambda ("args", "body")

# ListComp ("elt", "generators")

# List ("elts", "ctx")

# Load ()

# LShift ()

# Lt ()

# LtE ()

# Mod ()

# Module ("body",)

# Mult ()

# NameConstant ("value",)  # Py3 only
def do_NameConstant(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    if node.value is None:
        node = rebuilt(node, node.value)
        node.atype = nullable
    return node

# Name ("id", "ctx")
def do_Name(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    if isinstance(node.ctx, ast.Load):
        if node.id == "None":
            node = rebuilt(node, node.id, node.ctx)
            node.atype = nullable
            return node
        
        elif node.id not in symtable or symtable[node.id] is untracked:
            return node

        elif symtable[node.id].isparameter:
            return toliteral(0, lineno=node, atype=symtable[node.id])

        else:
            node = rebuilt(node, node.id, node.ctx)
            node.atype = symtable[node.id]
            return implicit(node, sym)

    else:
        return node

# Nonlocal ("names",)  # Py3 only

# Not ()

# NotEq ()

# NotIn ()

# Num ("n",)

# Or ()

# Param ()

# Pass ()

# Pow ()

# Print ("dest", "values", "nl")  # Py2 only

# Raise ("type", "inst", "tback")  # Py2
# Raise ("exc", "cause")           # Py3

# Repr ("value",)  # Py2 only

# Return ("value",)
def do_Return(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    value = recurse(node.value)

    if value.atype is untracked:      # TODO: also take this branch if in an externalfcn
        return rebuilt(node, value)

    elif value.atype is nullable:
        return rebuilt(node, value)

    else:
        def handler(schema):
            return tostmt("return TOPROXY(PARAMID, MEMBERID, INDEX)",
                          TOPROXY = toname(sym("ToProxy")),
                          PARAMID = toliteral(value.atype.parameter.index),
                          MEMBERID = toliteral(value.atype.parameter.reverse_members[id(schema)]),
                          INDEX = value)

        return value.atype.generate(handler)

# RShift ()

# SetComp ("elt", "generators")

# Set ("elts",)

# Slice ("lower", "upper", "step")

# Starred ("value", "ctx")  # Py3 only

# Store ()

# Str ("s",)

# Sub ()

# Subscript ("value", "slice", "ctx")
def do_Subscript(node, symtable, externalfcns, env, sym, sourcefile, recurse):
    node = rebuilt(node, recurse(node.value), recurse(node.slice), node.ctx)

    if node.value.atype is untracked:
        return node

    else:
        if not isinstance(node.slice, ast.Index):
            raise NotImplementedError

        def handler(schema):
            if isinstance(schema, List):
                beginarray = node.value.atype.parameter.require(schema, "beginarray", sym)
                endarray = node.value.atype.parameter.require(schema, "endarray", sym)

                if node.value.atype.nullable or schema.nullable:
                    outerindex = toexpr("REFUSENONE(INDEX)",
                                        REFUSENONE = toname(newrefusenone(env, sym, "list", node.value.lineno, sourcefile)),
                                        INDEX = node.value)
                else:
                    outerindex = node.value

                return toexpr("LISTGET(BEGIN, END, OUTERINDEX, INDEX)",
                              LISTGET = toname(sym("listget")),
                              BEGIN = toname(beginarray),
                              END = toname(endarray),
                              OUTERINDEX = outerindex,
                              INDEX = node.slice.value,
                              lineno = node,
                              atype = ArrowedType(schema.contents, node.value.atype.parameter))

            else:
                raise TypeError("object is not a list:\n\n{0}\n\nat line {lineno} of {sourcefile}".format(
                    schema.format("    "), lineno=node.lineno, sourcefile=sourcefile))

        return implicit(node.value.atype.generate(handler), sym)

# Suite ("body",)

# TryExcept ("body", "handlers", "orelse")         # Py2 only
# TryFinally ("body", "finalbody")                 # Py2 only
# Try ("body", "handlers", "orelse", "finalbody")  # Py3 only

# Tuple ("elts", "ctx")

# UAdd ()

# UnaryOp ("op", "operand")

# USub ()

# While ("test", "body", "orelse")

# withitem ("context_expr", "optional_vars")      # Py3 only
# With ("context_expr", "optional_vars", "body")  # Py2
# With ("items", "body")                          # Py3

# Yield ("value",)