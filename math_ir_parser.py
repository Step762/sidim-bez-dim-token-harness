"""Two offline MathIR compilers and a fail-closed semantic verifier.

The existing parser is an adapter, not rewritten solver logic. The second parser
is a small complete expression grammar; neither uses answers to infer semantics.
"""
import ast
from fractions import Fraction
import hashlib
import re

import math_solver as stable
from math_ir import Expr, Literal, MathIR, MathIRRefusal, UNIT_NAMES, bounded, execute, infer_unit, serialize, validate

MAX_INPUT = 8000
MAX_LITERAL = 80
TOKEN = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[A-Za-z_]+|[()+*/%,\-]')
NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def _digest(text):
    if type(text) is not str or not text or len(text)>MAX_INPUT:
        raise MathIRRefusal('invalid or oversized math instruction')
    try:return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError as exc:raise MathIRRefusal('invalid instruction encoding') from exc


def _literal(text, unit='one'):
    if len(text)>MAX_LITERAL:
        raise MathIRRefusal('literal length limit exceeded')
    if exponent:=re.search(r'[eE]([+-]?[0-9]+)$',text):
        if len(exponent[1].lstrip('+-'))>3 or abs(int(exponent[1]))>100:
            raise MathIRRefusal('decimal exponent limit exceeded')
    try:return Literal(bounded(Fraction(text)),unit)
    except (ValueError,ZeroDivisionError) as exc:raise MathIRRefusal('unsupported numeric literal') from exc


def _make(root, text, parser):
    program=MathIR(root,infer_unit(root),_digest(text),parser)
    validate(program)
    return program


def _expression_ast(parsed):
    tree,source=parsed
    def visit(node):
        if isinstance(node,ast.Expression):return visit(node.body)
        if isinstance(node,ast.Constant):return _literal(ast.get_source_segment(source,node))
        if isinstance(node,ast.UnaryOp):
            arg=visit(node.operand)
            if isinstance(node.op,ast.UAdd):return arg
            return Literal(-arg.value,arg.unit) if type(arg) is Literal else Expr('neg',(arg,))
        if isinstance(node,ast.BinOp):
            op={ast.Add:'add',ast.Sub:'sub',ast.Mult:'mul',ast.Div:'div'}.get(type(node.op))
            if op:return Expr(op,(visit(node.left),visit(node.right)))
        raise MathIRRefusal('unsupported existing expression node')
    return visit(tree)


def _unit_in_text(text):
    words='|'.join(sorted(UNIT_NAMES,key=len,reverse=True))
    found={UNIT_NAMES[m[1].lower()] for m in re.finditer(
        rf'(?<!\w)(?:{stable.NUMBER})\s+({words})(?!\w)',text,re.I)}
    if len(found)>1:raise MathIRRefusal('existing adapter cannot infer mixed units')
    return next(iter(found),'one')


def compile_existing(instruction):
    """A: translate the already supported parser structures without evaluating them."""
    _digest(instruction)
    try:kind,parsed=stable._request(instruction)
    except (ValueError,RecursionError,OverflowError) as exc:raise MathIRRefusal('existing parser refused instruction') from exc
    if kind=='expression':root=_expression_ast(parsed)
    elif kind=='binary':
        op,first,second=parsed;a,b=_expression_ast(first),_expression_ast(second)
        root=Expr('sub',(b,a)) if op=='reverse_sub' else Expr(op,(a,b))
    elif kind=='left_to_right':
        values,ops=parsed;root=Literal(values[0])
        for op,value in zip(ops,values[1:]):root=Expr({'+':'add','-':'sub','*':'mul','/':'div'}[op],(root,Literal(value)))
    elif kind=='round':root=Expr('round',(Literal(parsed[0]),Literal(Fraction(parsed[1]))),'half_up')
    elif kind=='power':
        base,exponent,tail,operand=parsed;root=Expr('power',(Literal(base),Literal(Fraction(exponent))))
        if tail:root=Expr({'plus':'add','minus':'sub','times':'mul','divided by':'div'}[tail],(root,Literal(operand)))
    elif kind=='steps':
        unit=_unit_in_text(instruction);initial,_,steps=parsed;root=Literal(initial,unit)
        for op,value,_ in steps:
            root=Expr(op,(root,Literal(value,unit if op in {'add','sub'} else 'one')))
    elif kind=='cost':
        count,price,fee,scope=parsed
        # Counted items and the price currency have different semantic roles.
        purchase=stable._COST.fullmatch(_body(instruction))
        if purchase is None:raise MathIRRefusal('purchase currency is not bound')
        unit=UNIT_NAMES[(purchase['currency'] or purchase['currency_each']).lower()]
        extra=Literal(fee,unit) if scope=='once' else Expr('mul',(Literal(count),Literal(fee,unit)))
        root=Expr('add',(Expr('mul',(Literal(count),Literal(price,unit))),extra))
    elif kind=='warehouse':
        if len(parsed['initial'])>MAX_LITERAL:raise MathIRRefusal('warehouse literal limit exceeded')
        root=_literal(parsed['initial'],'box')
        clauses=parsed['operations'][:-1].split('. ')
        if len(clauses)>64:raise MathIRRefusal('warehouse operation limit exceeded')
        for clause in clauses:
            if m:=stable._SPLIT.fullmatch(clause):root=Expr('floordiv',(root,_literal(m[1])))
            elif stable._DOUBLE.fullmatch(clause):root=Expr('mul',(root,Literal(Fraction(2))))
            elif m:=stable._SHIP.fullmatch(clause):root=Expr('sub',(root,Expr('mul',(_literal(m[1],'box'),_literal(m[2])))))
            elif m:=stable._RECEIVE.fullmatch(clause):root=Expr('add',(root,Expr('mul',(_literal(m[1]),_literal(m[2],'box')))))
            else:raise MathIRRefusal('unsupported warehouse clause')
    else:raise MathIRRefusal('unsupported existing parser structure')
    return _make(root,instruction,'existing')


def _body(instruction):
    _digest(instruction)
    text=' '.join(instruction.split())
    suffix=re.search(r'\s+(?:Compute it with|Work only inside|When you are done,)',text)
    if suffix:
        if stable._SUFFIX.fullmatch(text[suffix.start():]) is None:
            raise MathIRRefusal('unsupported extended runner directions')
        text=text[:suffix.start()]
    text=re.sub(r'^Solve the following arithmetic: ', '', text, flags=re.I)
    text=re.sub(r'^(?:What is|Calculate|Compute|Evaluate) ', '', text, flags=re.I)
    return text.rstrip('.?! ')


class _Grammar:
    """expr := product ((plus|minus|+|-) product)*; explicit parentheses delimit NL terms."""
    def __init__(self,text):
        self.tokens=[];position=0;self.position=0;self.depth=0
        while position<len(text):
            if text[position].isspace():position+=1;continue
            match=TOKEN.match(text,position)
            if match is None:raise MathIRRefusal('unsupported grammar token')
            self.tokens.append(match[0].lower());position=match.end()
            if len(self.tokens)>512:raise MathIRRefusal('token limit exceeded')

    def peek(self):return self.tokens[self.position] if self.position<len(self.tokens) else ''
    def take(self,expected=None):
        value=self.peek()
        if not value or expected is not None and value!=expected:raise MathIRRefusal('incomplete or unexpected grammar token')
        self.position+=1
        return value
    def expression(self):
        self.depth+=1
        if self.depth>32:raise MathIRRefusal('grammar nesting limit exceeded')
        try:
            root=self.product()
            while self.peek() in {'+','-','plus','minus'}:
                op=self.take();root=Expr('add' if op in {'+','plus'} else 'sub',(root,self.product()))
            return root
        finally:self.depth-=1
    def product(self):
        root=self.unary()
        while self.peek() in {'*','/','times','divided'}:
            op=self.take()
            if op=='divided':self.take('by')
            root=Expr('mul' if op in {'*','times'} else 'div',(root,self.unary()))
        return root
    def unary(self):
        if self.peek() in {'+','-','plus','minus'}:
            op=self.take()
            self.depth+=1
            if self.depth>32:raise MathIRRefusal('grammar unary limit exceeded')
            try:arg=self.unary()
            finally:self.depth-=1
            return arg if op in {'+','plus'} else Literal(-arg.value,arg.unit) if type(arg) is Literal else Expr('neg',(arg,))
        return self.primary()
    def group(self):
        self.take('(');root=self.expression();self.take(')');return root
    def primary(self):
        token=self.peek()
        if token=='(':return self.group()
        if NUMBER.fullmatch(token) or token in stable.WORDS:
            raw=self.take();root=Literal(Fraction(stable.WORDS[raw])) if raw in stable.WORDS else _literal(raw)
            if self.peek() in UNIT_NAMES:root=Literal(root.value,UNIT_NAMES[self.take()])
            if self.peek() in {'percent','%'}:
                if root.unit!='one':raise MathIRRefusal('rate has physical units')
                self.take();self.take('of');root=Expr('percent',(self.group(),root))
            return root
        if token in {'increase','decrease'}:
            self.take();value=self.group();self.take('by');rate=self.take()
            if not NUMBER.fullmatch(rate) and rate not in stable.WORDS:raise MathIRRefusal('literal percent rate required')
            number=Literal(Fraction(stable.WORDS[rate])) if rate in stable.WORDS else _literal(rate)
            if self.take() not in {'percent','%'}:raise MathIRRefusal('percent marker required')
            return Expr(token+'_percent',(value,number))
        if token=='convert':
            self.take();value=self.group();self.take('to');unit=self.take()
            if unit not in UNIT_NAMES:raise MathIRRefusal('unknown conversion unit')
            return Expr('convert',(value,),UNIT_NAMES[unit])
        if token=='round':
            self.take();value=self.group();self.take('to');raw=self.take()
            places=Literal(Fraction(stable.WORDS[raw])) if raw in stable.WORDS else _literal(raw)
            self.take('decimal')
            if self.take() not in {'place','places'}:raise MathIRRefusal('decimal places required')
            for expected in ('using','round','-','half','-','up'):self.take(expected)
            return Expr('round',(value,places),'half_up')
        if token=='the':
            self.take();name=self.take()
            if name=='absolute':self.take('value');name='abs'
            if name not in {'abs','mean','average','sum'}:raise MathIRRefusal('unsupported descriptive expression')
            self.take('of')
        else:name=self.take()
        operations={'abs':'abs','floor':'floor','mean':'mean','average':'mean','sum':'sum','ratio':'div','power':'power'}
        if name not in operations:raise MathIRRefusal('unsupported function name')
        self.take('(');args=[self.expression()]
        while self.peek()==',':self.take();args.append(self.expression())
        self.take(')')
        return Expr(operations[name],tuple(args))


def compile_grammar(instruction):
    """B: compose a fixed arithmetic language, consuming every non-runner token."""
    parser=_Grammar(_body(instruction))
    root=parser.expression()
    if parser.peek():raise MathIRRefusal('unconsumed mathematical instruction')
    return _make(root,instruction,'grammar')


def _commutative_key(node):
    """Proof key for validated exact IR; only binary add/mul may exchange sides.

    No evaluation, reassociation, cancellation or movement across rounding/unit
    conversion. Every literal, unit, operator, option and subtree is retained.
    """
    if type(node) is Literal:
        return ('literal', node.value.numerator, node.value.denominator, node.unit)
    children = tuple(_commutative_key(arg) for arg in node.args)
    if node.op in {'add', 'mul'}:
        children = tuple(sorted(children))
    return ('expression', node.op, node.option, children)


def verify(instruction, program):
    """Check structure/units and rebind the whole instruction, not just its numbers.

    The finite trusted compilers are the semantic admission boundary. This does
    not claim independent understanding of unrestricted natural language.
    """
    validate(program)
    if _digest(instruction)!=program.source_sha256:raise MathIRRefusal('instruction binding differs')
    expected=(compile_existing if program.parser=='existing' else compile_grammar)(instruction)
    metadata = lambda p: (p.output_unit, p.source_sha256, p.parser)
    if metadata(program)!=metadata(expected) or _commutative_key(program.root)!=_commutative_key(expected.root):
        raise MathIRRefusal('IR changes operations, literals, units, order or rounding')
    return program


def solve_math_ir(instruction):
    """Explicit experimental portfolio: stable success first; no production import."""
    try:return stable.solve_math(instruction)
    except stable.UnsupportedMath:pass
    errors=[]
    for compiler in (compile_grammar,compile_existing):
        try:
            program=verify(instruction,compiler(instruction))
            return serialize(execute(program),program)
        except MathIRRefusal as exc:errors.append(exc)
    raise stable.UnsupportedMath('experimental MathIR could not verify instruction') from None
