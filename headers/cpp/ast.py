#!/usr/bin/env python
#
# Copyright 2007 Neal Norwitz
# Portions Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generate an AST for C++."""

# TODO:
#  * Handle data/generic function/method declaration & definition (mostly done)
#  * Handle #include somewhere.
#  * Handle casts (both C++ and C-style)
#  * Handle conditions and loops (if/else, switch, for, while/do)
#
# TODO much, much later:
#  * Handle #define
#  * exceptions

import sys
import traceback

from cpp import keywords
from cpp import tokenize
from cpp import utils


# Set to True to see the start/end token indices.
DEBUG = True


VISIBILITY_PUBLIC, VISIBILITY_PROTECTED, VISIBILITY_PRIVATE = range(3)

FUNCTION_CONST = 0x01
FUNCTION_VIRTUAL = 0x02
FUNCTION_PURE_VIRTUAL = 0x04
FUNCTION_CTOR = 0x08
FUNCTION_DTOR = 0x10

_INTERNAL_TOKEN = 'internal'
_NAMESPACE_POP = 'ns-pop'

_WHENCE_STREAM, _WHENCE_QUEUE = range(2)


# TODO(nnorwitz): Move Token into the tokenize module.
class Token(object):
    def __init__(self, token_type, name, start, end):
        self.token_type = token_type
        self.name = name
        self.start = start
        self.end = end
        self.whence = _WHENCE_STREAM
    def __str__(self):
        if not DEBUG:
            return 'Token(%r)' % self.name
        return 'Token(%r, %s, %s)' % (self.name, self.start, self.end)
    __repr__ = __str__


# TODO(nnorwitz): move AST nodes into a separate module.
class Node(object):
    """Base AST node."""
    def __init__(self, start, end):
        self.start = start
        self.end = end
    def IsDeclaration(self):
        return False
    def IsDefinition(self):
        return False
    def Requires(self, node):
        """Does this AST node require the definition of the node passed in?"""
        return False
    def XXX__str__(self):
        return self._StringHelper(self.__class__.__name__, '')
    def _StringHelper(self, name, suffix):
        if not DEBUG:
            return '%s(%s)' % (name, suffix)
        return '%s(%d, %d, %s)' % (name, self.start, self.end, suffix)
    def __repr__(self):
        return str(self)


class Define(Node):
    def __init__(self, start, end, name, definition):
        Node.__init__(self, start, end)
        self.name = name
        self.definition = definition
    def __str__(self):
        value = '%s %s' % (self.name, self.definition)
        return self._StringHelper(self.__class__.__name__, value)


class Include(Node):
    def __init__(self, start, end, filename, system):
        Node.__init__(self, start, end)
        self.filename = filename
        self.system = system
    def __str__(self):
        fmt = '"%s"'
        if self.system:
            fmt = '<%s>'
        return self._StringHelper(self.__class__.__name__, fmt % self.filename)


class Goto(Node):
    def __init__(self, start, end, label):
        Node.__init__(self, start, end)
        self.label = label
    def __str__(self):
        return self._StringHelper(self.__class__.__name__, str(self.label))


class Expr(Node):
    def __init__(self, start, end, expr):
        Node.__init__(self, start, end)
        self.expr = expr
    def Requires(self, node):
        # TODO(nnorwitz): impl.
        return False
    def __str__(self):
        return self._StringHelper(self.__class__.__name__, str(self.expr))


class Return(Expr):
    pass

class Delete(Expr):
    pass

class Friend(Expr):
    pass


class Using(Node):
    def __init__(self, start, end, names):
        Node.__init__(self, start, end)
        self.names = names
    def __str__(self):
        return self._StringHelper(self.__class__.__name__, str(self.names))


class Parameter(Node):
    def __init__(self, start, end, name, type_name, type_modifiers,
                 reference, pointer, default):
        Node.__init__(self, start, end)
        self.name = name
        self.type_name = type_name
        self.type_modifiers = type_modifiers
        self.reference = reference
        self.pointer = pointer
        self.default = default
    def Requires(self, node):
        # TODO(nnorwitz): handle namespaces, etc.
        return self.type_name == node.name
    def __str__(self):
        modifiers = ' '.join(self.type_modifiers)
        syntax = ''
        if self.reference:
            syntax += '&'
        if self.pointer:
            syntax += '*'
        suffix = '%s %s%s %s' % (modifiers, self.type_name, syntax, self.name)
        if self.default:
            suffix += ' = ' + self.default
        return self._StringHelper(self.__class__.__name__, suffix)


def _DeclarationToParts(parts):
    name = parts.pop()
    modifiers = []
    type_name = []
    for p in parts:
        if keywords.IsKeyword(p):
            modifiers.append(p)
        elif p == '<':
            # Ignore the template portion, we know that must be used.
            # TODO(nnorwitz): we really need to keep the templated name
            # separately so we know to keep the header that included it.
            type_name.pop()
        elif p not in ('*', '&', '>'):
            type_name.append(p)
    type_name = ''.join(type_name)
    return name, type_name, modifiers


def _SequenceToParameters(seq):
    if not seq:
        return []

    result = []
    name = type_name = ''
    type_modifiers = []
    pointer = reference = False
    first_token = default = None
    for s in seq:
        if not first_token:
            first_token = s
        if s.name == ',':
            # TODO(nnorwitz): handle default values.
            name, type_name, modifiers = _DeclarationToParts(type_modifiers)
            p = Parameter(first_token.start, first_token.end, name, type_name,
                          modifiers, reference, pointer, default)
            result.append(p)
            name = type_name = ''
            type_modifiers = []
            pointer = reference = False
            first_token = default = None
        elif s.name == '*':
            pointer = True
        elif s.name == '&':
            reference = True
        else:
            type_modifiers.append(s.name)
    name, type_name, modifiers = _DeclarationToParts(type_modifiers)
    p = Parameter(first_token.start, first_token.end, name, type_name,
                  modifiers, reference, pointer, default)
    result.append(p)
    return result


class _GenericDeclaration(Node):
    def __init__(self, start, end, name, namespace):
        Node.__init__(self, start, end)
        self.name = name
        self.namespace = namespace[:]
    def FullName(self):
        prefix = ''
        if self.namespace:
            prefix = '::'.join(self.namespace) + '::'
        return prefix + self.name
    def _TypeStringHelper(self, suffix):
        if self.namespace:
            names = [n or '<anonymous>' for n in self.namespace]
            suffix += ' in ' + '::'.join(names)
        return self._StringHelper(self.__class__.__name__, suffix)


# TODO(nnorwitz): merge with Parameter in some way?
class VariableDeclaration(_GenericDeclaration):
    def __init__(self, start, end, name, type_name, type_modifiers,
                 reference, pointer, initial_value):
        _GenericDeclaration.__init__(self, start, end, name, [])
        self.type_name = type_name
        self.type_modifiers = type_modifiers
        self.reference = reference
        self.pointer = pointer
        self.initial_value = initial_value
    def Requires(self, node):
        # TODO(nnorwitz): handle namespaces, etc.
        return self.type_name == node.name
    def __str__(self):
        modifiers = ' '.join(self.type_modifiers)
        syntax = ''
        if self.reference:
            syntax += '&'
        if self.pointer:
            syntax += '*'
        suffix = '%s %s%s %s' % (modifiers, self.type_name, syntax, self.name)
        if self.initial_value:
            suffix += ' = ' + self.initial_value
        return self._StringHelper(self.__class__.__name__, suffix)


class Typedef(_GenericDeclaration):
    def __init__(self, start, end, name, alias, namespace):
        _GenericDeclaration.__init__(self, start, end, name, namespace)
        self.alias = alias
    def IsDefinition(self):
        return True
    def Requires(self, node):
        # TODO(nnorwitz): handle namespaces, etc.
        name = node.name
        for token in self.alias:
            if token is not None and name == token.name:
                return True
        return False
    def __str__(self):
        suffix = '%s, %s' % (self.name, self.alias)
        return self._TypeStringHelper(suffix)


class _NestedType(_GenericDeclaration):
    def __init__(self, start, end, name, fields, namespace):
        _GenericDeclaration.__init__(self, start, end, name, namespace)
        self.fields = fields
    def IsDefinition(self):
        return True
    def __str__(self):
        suffix = '%s, {%s}' % (self.name, self.fields)
        return self._TypeStringHelper(suffix)


class Union(_NestedType):
    pass
class Enum(_NestedType):
    pass


class Class(_GenericDeclaration):
    def __init__(self, start, end, name, bases, body, namespace):
        _GenericDeclaration.__init__(self, start, end, name, namespace)
        self.bases = bases
        self.body = body
    def IsDeclaration(self):
        return self.bases is None and self.body is None
    def IsDefinition(self):
        return not self.IsDeclaration()
    def Requires(self, node):
        # TODO(nnorwitz): handle namespaces, etc.
        if self.bases:
            for token_list in self.bases:
                # TODO(nnorwitz) bases are tokens, do name comparision.
                for token in token_list:
                    if token.name == node.name:
                        return True
        # TODO(nnorwitz): search in body too.
        return False
    def __str__(self):
        suffix = '%s, %s, %s' % (self.name, self.bases, self.body)
        return self._TypeStringHelper(suffix)


class Struct(Class):
    pass


class Function(_GenericDeclaration):
    def __init__(self, start, end, name, return_type, parameters,
                 modifiers, body, namespace):
        _GenericDeclaration.__init__(self, start, end, name, namespace)
        if not return_type:
            return_type = None
        self.return_type = return_type
        self.parameters = parameters
        self.modifiers = modifiers
        self.body = body
    def IsDefinition(self):
        return True
    def Requires(self, node):
        if self.parameters:
            # TODO(nnorwitz) parameters are tokens, do name comparision.
            for p in self.parameters:
                if p.name == node.name:
                    return True
        # TODO(nnorwitz): search in body too.
        return False
    def __str__(self):
        suffix = ('%s %s(%s), 0x%02x, %s' %
                  (self.return_type, self.name, self.parameters,
                   self.modifiers, self.body))
        return self._TypeStringHelper(suffix)


class AstBuilder(object):
    def __init__(self, token_stream, filename, in_class='', visibility=None):
        self.tokens = token_stream
        self.filename = filename
        self.token_queue = []
        self.namespace_stack = []
        self.in_class = in_class
        self.visibility = visibility
        self.in_function = False
        self.current_start = None
        self.current_end = None

    def Generate(self):
        while 1:
            token = self._GetNextToken()
            if not token:
                break

            # Get the next token.
            self.current_start = token.start
            self.current_end = token.end

            # Dispatch on the next token type.
            if token.token_type == _INTERNAL_TOKEN:
                if token.name == _NAMESPACE_POP:
                    self.namespace_stack.pop()
                continue

            try:
                result = self._GenerateOne(token)
                if result is not None:
                    yield result
            except:
                q = self.token_queue
                print >>sys.stderr, \
                          'Got exception in', self.filename, '@', token, q
                raise

    def _GenerateOne(self, token):
        if token.token_type == tokenize.NAME:
            if (keywords.IsKeyword(token.name) and
                not keywords.IsBuiltinType(token.name)):
                method = getattr(self, 'handle_' + token.name)
                return method()
            elif token == self.in_class:
                # The token is the class we are in, must be a ctor.
                return self._GetMethod(token, FUNCTION_CTOR)
            else:
                # Handle data or function declaration/definition.
                temp_tokens, last_token = \
                    self._GetVarTokensUpTo(tokenize.SYNTAX, '(', ';', '{')
                temp_tokens.insert(0, token)
                if last_token.name == '(':
                    # If there is an assignment before a paren, this is an
                    # expression, not a method.
                    expr = bool([e for e in temp_tokens if e.name == '='])
                    if expr:
                        new_temp = self._GetTokensUpTo(tokenize.SYNTAX, ';')
                        temp_tokens.append(last_token)
                        temp_tokens.extend(new_temp)
                        last_token = Token(tokenize.SYNTAX, ';', 0, 0)

                if last_token.name == ';':
                    # Handle data, this isn't a method.
                    names = [t.name for t in temp_tokens]
                    name, type_name, modifiers = \
                          _DeclarationToParts(names)
                    t0 = temp_tokens[0]
                    reference = '&' in names
                    pointer = '*' in names
                    value = None
                    return VariableDeclaration(t0.start, t0.end, name,
                                               type_name, modifiers,
                                               reference, pointer, value)
                if last_token.name == '{':
                    self._AddBackTokens(temp_tokens[1:])
                    self._AddBackToken(last_token)
                    method_name = temp_tokens[0].name
                    method = getattr(self, 'handle_' + method_name, None)
                    if not method:
                        # Must be declaring a variable.
                        # TODO(nnorwitz): handle the declaration.
                        return None
                    return method()
                return self._GetMethod(temp_tokens, 0, False)
        elif token.token_type == tokenize.SYNTAX:
            if token.name == '~' and self.in_class:
                # Must be a dtor (probably not in method body).
                token = self._GetNextToken()
                if (token.token_type == tokenize.NAME and
                    token.name == self.in_class):
                    return self._GetMethod([token], FUNCTION_DTOR)
            # TODO(nnorwitz); handle a lot more syntax.
        elif token.token_type == tokenize.PREPROCESSOR:
            # TODO(nnorwitz): handle more preprocessor directives.
            # token starts with a #, so remove it and strip whitespace.
            name = token.name[1:].lstrip()
            if name.startswith('include'):
                # Remove "include".
                name = name[7:].strip()
                assert name
                assert name[0] in '<"', token
                assert name[-1] in '>"', token
                system = name[0] == '<'
                filename = name[1:-1]
                return Include(token.start, token.end, filename, system)
            if name.startswith('define'):
                # Remove "define".
                name = name[6:].strip()
                assert name
                value = ''
                for i, c in enumerate(name):
                    if c.isspace():
                        value = name[i:].lstrip()
                        name = name[:i]
                        break
                return Define(token.start, token.end, name, value)
        return None

    def _GetTokensUpTo(self, expected_token_type, expected_token):
        return self._GetVarTokensUpTo(expected_token_type, expected_token)[0]

    def _GetVarTokensUpTo(self, expected_token_type, *expected_tokens):
        last_token = self._GetNextToken()
        tokens = []
        while (last_token.token_type != expected_token_type or
               last_token.name not in expected_tokens):
            tokens.append(last_token)
            last_token = self._GetNextToken()
        return tokens, last_token

    # TODO(nnorwitz): remove _IgnoreUpTo() it shouldn't be necesary.
    def _IgnoreUpTo(self, token_type, token):
        unused_tokens = self._GetTokensUpTo(token_type, token)

    def _GetMatchingChar(self, open_paren, close_paren):
        # Assumes the current token is open_paren and we will consume
        # and return up to the close_paren.
        count = 1
        token = self._GetNextToken()
        while 1:
            if token.token_type == tokenize.SYNTAX:
                if token.name == open_paren:
                    count += 1
                elif token.name == close_paren:
                    count -= 1
                    if count == 0:
                        break
            yield token
            token = self._GetNextToken()

    def _GetParameters(self):
        return self._GetMatchingChar('(', ')')

    def GetScope(self):
        return self._GetMatchingChar('{', '}')

    def _GetNextToken(self):
        if self.token_queue:
            # TODO(nnorwitz): use a better data structure for the queue.
            return self.token_queue.pop(0)
        next = self.tokens.next()
        if isinstance(next, Token):
            return next
        return Token(*next)

    def _AddBackToken(self, token):
        if token.whence == _WHENCE_STREAM:
            token.whence = _WHENCE_QUEUE
            self.token_queue.append(token)
        else:
            assert token.whence == _WHENCE_QUEUE, token
            self.token_queue.insert(0, token)

    def _AddBackTokens(self, tokens):
        if tokens:
            if tokens[0].whence == _WHENCE_STREAM:
                for token in tokens:
                    token.whence = _WHENCE_QUEUE
                self.token_queue.extend(tokens)
            else:
                assert tokens[0].whence == _WHENCE_QUEUE, tokens
                self.token_queue[:0] = tokens

    def GetName(self):
        """Returns ([tokens], next_token_info)."""
        next_token = self._GetNextToken()
        tokens = []
        while (next_token.token_type == tokenize.NAME or
               (next_token.token_type == tokenize.SYNTAX and
                next_token.name in ('::', '<'))):
            tokens.append(next_token)
            # Handle templated names.
            if next_token.name == '<':
                tokens.extend(self._GetMatchingChar('<', '>'))
            next_token = self._GetNextToken()
        return tokens, next_token

    def GetMethod(self, modifiers=0):
        return_type_and_name = self._GetTokensUpTo(tokenize.SYNTAX, '(')
        assert len(return_type_and_name) >= 1
        return self._GetMethod(return_type_and_name, modifiers, False)

    def _GetMethod(self, return_type_and_name, modifiers, get_paren=True):
        if get_paren:
            token = self._GetNextToken()
            assert token.token_type == tokenize.SYNTAX, token
            assert token.name == '(', token

        name = return_type_and_name.pop()
        return_type = return_type_and_name
        indices = name
        if return_type:
            indices = return_type[0]

        parameters = list(self._GetParameters())

        # Handling operator() is especially weird.
        if name.name == 'operator' and not parameters:
            token = self._GetNextToken()
            assert token.name == '(', token
            parameters = list(self._GetParameters())

        token = self._GetNextToken()
        if token.token_type == tokenize.NAME:
            # TODO(nnorwitz): will eventually need to handle __attribute__.
            assert token.name == 'const', token
            modifiers += FUNCTION_CONST
            token = self._GetNextToken()

        assert token.token_type == tokenize.SYNTAX, token
        # Handle ctor initializers.
        if token.name == ':':
            # TODO(nnorwitz): anything else to handle for initializer list?
            while token.name != ';' and token.name != '{':
                token = self._GetNextToken()

        # Handle pointer to functions that are really data but looked
        # like method declarations.
        if token.name == '(':
            if parameters[0].name == '*':
                # name contains the return type.
                name = parameters.pop()
                # parameters contains the name of the data.
                modifiers = [p.name for p in parameters]
                # Already at the ( to open the parameter list.
                function_parameters = list(self._GetMatchingChar('(', ')'))
                # TODO(nnorwitz): store the function_parameters.
                token = self._GetNextToken()
                assert token.token_type == tokenize.SYNTAX, token
                assert token.name == ';', token
                return VariableDeclaration(indices.start, indices.end,
                                           name.name, indices.name, modifiers,
                                           reference=False, pointer=False,
                                           initial_value=None)
            # At this point, we got something like:
            #  return_type (type::*name_)(params);
            # This is a data member called name_ that is a function pointer.
            # With this code: void (sq_type::*field_)(string&);
            # We get: name=void return_type=[] parameters=sq_type ... field_
            # TODO(nnorwitz): is return_type always empty?
            # TODO(nnorwitz): this isn't even close to being correct.
            # Just put in something so we don't crash and can move on.
            real_name = parameters[-1]
            modifiers = [p.name for p in self._GetParameters()]
            return VariableDeclaration(indices.start, indices.end,
                                       real_name.name, indices.name, modifiers,
                                       reference=False, pointer=False,
                                       initial_value=None)

        if token.name == '{':
            body = list(self.GetScope())
        else:
            body = None
            if token.name == '=':
                token = self._GetNextToken()
                assert token.token_type == tokenize.CONSTANT, token
                assert token.name == '0', token
                modifiers += FUNCTION_PURE_VIRTUAL
                token = self._GetNextToken()

            assert token.name == ';', (token, return_type_and_name, parameters)

        return Function(indices.start, indices.end,
                        name.name, return_type, parameters, modifiers, body,
                        self.namespace_stack)

    def handle_bool(self):
        pass

    def handle_char(self):
        pass

    def handle_int(self):
        pass

    def handle_long(self):
        pass

    def handle_short(self):
        pass

    def handle_double(self):
        pass

    def handle_float(self):
        pass

    def handle_void(self):
        pass

    def handle_wchar_t(self):
        pass

    def handle_unsigned(self):
        pass

    def handle_signed(self):
        pass

    def _GetNestedType(self, ctor):
        name = None
        token = self._GetNextToken()
        if token.token_type == tokenize.NAME:
            name = token.name
            token = self._GetNextToken()

        assert token.token_type == tokenize.SYNTAX, token
        assert token.name == '{', token
        fields = list(self._GetMatchingChar('{', '}'))
        return ctor(token.start, token.end, name, fields, self.namespace_stack)

    def handle_struct(self):
        return self._GetClass(Struct, VISIBILITY_PUBLIC, False)

    def handle_union(self):
        return self._GetNestedType(Union)

    def handle_enum(self):
        return self._GetNestedType(Enum)

    def handle_auto(self):
        pass

    def handle_register(self):
        pass

    def handle_const(self):
        pass

    def handle_inline(self):
        pass

    def handle_extern(self):
        pass

    def handle_static(self):
        pass

    def handle_virtual(self):
        # What follows must be a method.
        token = self._GetNextToken()
        if token.token_type == tokenize.SYNTAX:
            # Better be a virtual dtor
            assert token.name == '~', token
            return self.GetMethod(FUNCTION_VIRTUAL + FUNCTION_DTOR)
        assert token.token_type == tokenize.NAME, token
        return_type_and_name = self._GetTokensUpTo(tokenize.SYNTAX, '(')
        return_type_and_name.insert(0, token)
        return self._GetMethod(return_type_and_name, FUNCTION_VIRTUAL, False)

    def handle_volatile(self):
        pass

    def handle_mutable(self):
        pass

    def handle_public(self):
        assert self.in_class
        self.visibility = VISIBILITY_PUBLIC

    def handle_protected(self):
        assert self.in_class
        self.visibility = VISIBILITY_PROTECTED

    def handle_private(self):
        assert self.in_class
        self.visibility = VISIBILITY_PRIVATE

    def handle_friend(self):
        tokens = self._GetTokensUpTo(tokenize.SYNTAX, ';')
        assert tokens
        return Friend(tokens[0].start, tokens[0].end, tokens)

    def handle_static_cast(self):
        pass

    def handle_const_cast(self):
        pass

    def handle_dynamic_cast(self):
        pass

    def handle_reinterpret_cast(self):
        pass

    def handle_new(self):
        pass

    def handle_delete(self):
        tokens = self._GetTokensUpTo(tokenize.SYNTAX, ';')
        assert tokens
        return Delete(tokens[0].start, tokens[0].end, tokens)

    def handle_typedef(self):
        token = self._GetNextToken()
        if (token.token_type == tokenize.NAME and
            keywords.IsKeyword(token.name)):
            # Token must be struct/enum/union.
            method = getattr(self, 'handle_' + token.name)
            tokens = [method()]
        else:
            tokens = [token]

        # Get the remainder of the typedef up to the semi-colon.
        tokens.extend(self._GetTokensUpTo(tokenize.SYNTAX, ';'))

        assert tokens
        name = tokens.pop()
        indices = name
        if tokens:
            indices = tokens[0]
        if not indices:
            indices = token
        # TODO(nnorwitz): handle pointers to functions properly
        return Typedef(indices.start, indices.end, name.name, tokens,
                       self.namespace_stack)

    def handle_typeid(self):
        pass  # Not needed yet.

    def handle_typename(self):
        pass  # Not needed yet.

    def handle_template(self):
        token = self._GetNextToken()
        assert token.token_type == tokenize.SYNTAX, token
        assert token.name == '<', token
        template_params = list(self._GetMatchingChar('<', '>'))
        # TODO(nnorwitz): for now, just ignore the template params.
        token = self._GetNextToken()
        if token.token_type == tokenize.NAME and token.name == 'class':
            return self.handle_class()
        self._AddBackToken(token)
        return self.GetMethod()

    def handle_true(self):
        pass  # Nothing to do.

    def handle_false(self):
        pass  # Nothing to do.

    def handle_asm(self):
        pass  # Not needed yet.

    def handle_class(self):
        return self._GetClass(Class, VISIBILITY_PRIVATE, True)

    def _GetClass(self, class_type, visibility, get_last_token):
        class_name = None
        class_token = self._GetNextToken()
        if class_token.token_type != tokenize.NAME:
            assert class_token.token_type == tokenize.SYNTAX, class_token
            token = class_token
        else:
            class_name = class_token.name
            token = self._GetNextToken()
            # Handle class names like:  Foo::Bar
            while token.token_type == tokenize.SYNTAX and token.name == '::':
                token = self._GetNextToken()
                assert token.token_type == tokenize.NAME, token
                class_name += '::' + token.name
                token = self._GetNextToken()
        bases = None
        if token.token_type == tokenize.SYNTAX:
            if token.name == ';':
                # Forward declaration.
                return class_type(class_token.start, class_token.end,
                                  class_name, None, None,
                                  self.namespace_stack)
            if token.name == ':':
                # Get base classes.
                bases = []
                while 1:
                    token = self._GetNextToken()
                    assert token.token_type == tokenize.NAME, token
                    # TODO(nnorwitz): handle private inheritance...maybe.
                    assert token.name in ('public', 'protected', 'private'), token
                    base, next_token = self.GetName()
                    bases.append(base)
                    assert next_token.token_type == tokenize.SYNTAX, next_token
                    if next_token.name == '{':
                        token = next_token
                        break
                    # Support multiple inheritance.
                    assert next_token.name == ',', next_token

        assert token.token_type == tokenize.SYNTAX, token
        assert token.name == '{', token

        ast = AstBuilder(self.GetScope(), self.filename, class_name,
                         visibility)
        body = list(ast.Generate())

        if get_last_token:
            token = self._GetNextToken()
            assert token.token_type == tokenize.SYNTAX, token
            assert token.name == ';', token

        return class_type(class_token.start, class_token.end, class_name,
                          bases, body, self.namespace_stack)

    def handle_namespace(self):
        token = self._GetNextToken()
        # Support anonymous namespaces.
        name = None
        if token.token_type == tokenize.NAME:
            name = token.name
            token = self._GetNextToken()
        self.namespace_stack.append(name)
        assert token.token_type == tokenize.SYNTAX, token
        if token.name == '=':
            # TODO(nnorwitz): handle aliasing namespaces.
            name, next_token = self.GetName()
            assert next_token.name == ';', next_token
            return None
        assert token.name == '{', token
        tokens = list(self.GetScope())
        # Handle namespace with nothing in it.
        if tokens:
            self._AddBackTokens(tokens)
        self._AddBackToken(Token(_INTERNAL_TOKEN, _NAMESPACE_POP, None, None))
        return None

    def handle_using(self):
        tokens = self._GetTokensUpTo(tokenize.SYNTAX, ';')
        assert tokens
        return Using(tokens[0].start, tokens[0].end, tokens)

    def handle_explicit(self):
        assert self.in_class
        # Nothing much to do.
        # TODO(nnorwitz): maybe verify the method name == class name.
        # This must be a ctor.
        return self.GetMethod(FUNCTION_CTOR)

    def handle_this(self):
        pass  # Nothing to do.

    def handle_operator(self):
        # Pull off the next token(s?) and make that part of the method name.
        pass

    def handle_sizeof(self):
        pass

    def handle_case(self):
        pass

    def handle_switch(self):
        pass

    def handle_default(self):
        token = self._GetNextToken()
        assert token.token_type == tokenize.SYNTAX
        assert token.name == ':'

    def handle_if(self):
        pass

    def handle_else(self):
        pass

    def handle_return(self):
        tokens = self._GetTokensUpTo(tokenize.SYNTAX, ';')
        if not tokens:
            return Return(self.current_start, self.current_end, None)
        return Return(tokens[0].start, tokens[0].end, tokens)

    def handle_goto(self):
        tokens = self._GetTokensUpTo(tokenize.SYNTAX, ';')
        assert len(tokens) == 1, str(tokens)
        return Goto(tokens[0].start, tokens[0].end, tokens[0].name)

    def handle_try(self):
        pass  # Not needed yet.

    def handle_catch(self):
        pass  # Not needed yet.

    def handle_throw(self):
        pass  # Not needed yet.

    def handle_while(self):
        pass

    def handle_do(self):
        pass

    def handle_for(self):
        pass

    def handle_break(self):
        self._IgnoreUpTo(tokenize.SYNTAX, ';')

    def handle_continue(self):
        self._IgnoreUpTo(tokenize.SYNTAX, ';')


def BuilderFromSource(source, filename):
    return AstBuilder(tokenize.GetTokens(source), filename)


def PrintIndentifiers(filename, should_print):
    source = utils.ReadFile(filename, False)
    if source is None:
        print 'Unable to find', filename
        return

    #print 'Processing', actual_filename
    builder = BuilderFromSource(source, filename)
    try:
        for node in builder.Generate():
            if should_print(node):
                print node.name
    except:
        pass


def PrintAllIndentifiers(filenames, should_print):
    for path in filenames:
        PrintIndentifiers(path, should_print)


def main(argv):
    for filename in argv[1:]:
        source = utils.ReadFile(filename)
        if source is None:
            continue

        print 'Processing', filename
        builder = BuilderFromSource(source, filename)
        try:
            entire_ast = filter(None, builder.Generate())
        except:
            # Already printed a warning, print the traceback and continue.
            traceback.print_exc()
        else:
            if DEBUG:
                for ast in entire_ast:
                    print ast


if __name__ == '__main__':
    main(sys.argv)
