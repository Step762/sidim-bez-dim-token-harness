"""Experimental typed arithmetic IR: exact values, explicit units and bounded execution.

No production solver imports this module. Text compilation is in math_ir_parser.
"""
from dataclasses import dataclass
from fractions import Fraction
import time

from math_solver import format_math

MAX_NODES = 256
MAX_OPERATIONS = 64
MAX_DEPTH = 32
MAX_BITS = 2048
MAX_POWER = 64
MAX_PLACES = 100
MAX_SECONDS = 1.0


class MathIRRefusal(ValueError):
    pass


# Canonical unit -> dimension, exact scale. Currency exchange is never inferred.
UNITS = {
    'one': ('scalar', Fraction(1)),
    'm': ('length', Fraction(1)), 'cm': ('length', Fraction(1,100)),
    'km': ('length', Fraction(1000)),
    'l': ('volume', Fraction(1)), 'ml': ('volume', Fraction(1,1000)),
    's': ('time', Fraction(1)), 'min': ('time', Fraction(60)), 'h': ('time', Fraction(3600)),
    **{name:(name,Fraction(1)) for name in ('euro','dollar','pound','yen','box','item','unit','coin','point','apple')},
}
UNIT_NAMES = {
    'meter':'m','meters':'m','metre':'m','metres':'m','m':'m',
    'centimeter':'cm','centimeters':'cm','centimetre':'cm','centimetres':'cm','cm':'cm',
    'kilometer':'km','kilometers':'km','kilometre':'km','kilometres':'km','km':'km',
    'litre':'l','litres':'l','liter':'l','liters':'l','l':'l',
    'millilitre':'ml','millilitres':'ml','milliliter':'ml','milliliters':'ml','ml':'ml',
    'second':'s','seconds':'s','s':'s','minute':'min','minutes':'min','min':'min',
    'hour':'h','hours':'h','h':'h',
    'euro':'euro','euros':'euro','dollar':'dollar','dollars':'dollar',
    'pound':'pound','pounds':'pound','yen':'yen',
    **{spelling:name for name in ('box','item','coin','point','apple')
       for spelling in (name, 'boxes' if name=='box' else name+'s')},
    'unit':'unit','units':'unit',
}


@dataclass(frozen=True)
class Literal:
    value: Fraction
    unit: str = 'one'


@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple
    option: str = ''


@dataclass(frozen=True)
class MathIR:
    root: Literal | Expr
    output_unit: str
    source_sha256: str
    parser: str


@dataclass(frozen=True)
class Quantity:
    value: Fraction
    unit: str


def bounded(value):
    if type(value) is not Fraction or max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise MathIRRefusal('invalid or oversized exact value')
    return value


def infer_unit(root):
    """Validate the entire tree, operation signatures and dimensions before execution."""
    nodes = operations = 0
    started = time.monotonic()

    def visit(node, depth):
        nonlocal nodes, operations
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH or time.monotonic()-started > MAX_SECONDS:
            raise MathIRRefusal('IR structure limit exceeded')
        if type(node) is Literal:
            bounded(node.value)
            if type(node.unit) is not str or node.unit not in UNITS:
                raise MathIRRefusal('unknown literal unit')
            return node.unit
        if type(node) is not Expr or type(node.args) is not tuple or type(node.op) is not str or type(node.option) is not str:
            raise MathIRRefusal('invalid IR node')
        operations += 1
        if operations > MAX_OPERATIONS:
            raise MathIRRefusal('IR operation limit exceeded')
        arities = {'add':2,'sub':2,'mul':2,'div':2,'floordiv':2,'neg':1,'abs':1,'floor':1,
                   'percent':2,'increase_percent':2,'decrease_percent':2,'power':2,'round':2,'convert':1}
        if node.op in {'mean','sum'}:
            if not 2 <= len(node.args) <= 16:
                raise MathIRRefusal('unsupported aggregate arity')
        elif node.op not in arities or len(node.args) != arities[node.op]:
            raise MathIRRefusal('unsupported operation or arity')
        units = [visit(arg,depth+1) for arg in node.args]
        if node.op == 'convert':
            if node.option not in UNITS or UNITS[units[0]][0] != UNITS[node.option][0]:
                raise MathIRRefusal('unsupported unit conversion')
            return node.option
        if node.op == 'round':
            places = node.args[1]
            if (node.option != 'half_up' or type(places) is not Literal or places.unit != 'one'
                or places.value.denominator != 1 or not 0 <= places.value <= MAX_PLACES):
                raise MathIRRefusal('rounding requires an explicit supported mode and precision')
            return units[0]
        if node.option:
            raise MathIRRefusal('unexpected operation option')
        if node.op in {'add','sub','mean','sum'}:
            if len(set(units)) != 1:
                raise MathIRRefusal('explicit conversion required before combining units')
            return units[0]
        if node.op == 'mul':
            if units[0] != 'one' and units[1] != 'one':
                raise MathIRRefusal('compound multiplication units are unsupported')
            return units[1] if units[0]=='one' else units[0]
        if node.op in {'div','floordiv'}:
            if units[1]=='one':return units[0]
            if units[0]==units[1]:return 'one'
            raise MathIRRefusal('incompatible quotient units')
        if node.op in {'percent','increase_percent','decrease_percent'}:
            if units[1] != 'one':
                raise MathIRRefusal('percentage rate must be dimensionless')
            return units[0]
        if node.op == 'power':
            exponent = node.args[1]
            if (units != ['one','one'] or type(exponent) is not Literal
                or exponent.value.denominator != 1 or abs(exponent.value)>MAX_POWER):
                raise MathIRRefusal('unsupported exponent or power dimensions')
            return 'one'
        return units[0]  # abs, neg, floor preserve the explicit unit.
    return visit(root,0)


def validate(program):
    if (type(program) is not MathIR or type(program.parser) is not str or program.parser not in {'existing','grammar'}
        or type(program.source_sha256) is not str or len(program.source_sha256)!=64
        or any(c not in '0123456789abcdef' for c in program.source_sha256)):
        raise MathIRRefusal('invalid program envelope')
    if infer_unit(program.root) != program.output_unit:
        raise MathIRRefusal('output dimension differs from expression')


def execute(program, *, timeout=MAX_SECONDS):
    """Arithmetic has no access to task text, files, network or model code."""
    if type(timeout) not in {int,float} or not 0 < timeout <= MAX_SECONDS:
        raise MathIRRefusal('invalid execution time allowance')
    validate(program)
    deadline = time.monotonic()+timeout

    def visit(node):
        if time.monotonic() >= deadline:
            raise MathIRRefusal('IR execution deadline reached')
        if type(node) is Literal:
            return Quantity(node.value,node.unit)
        args = [visit(arg) for arg in node.args]
        values = [a.value for a in args]
        unit = args[0].unit
        if node.op=='add':value=values[0]+values[1]
        elif node.op=='sub':value=values[0]-values[1]
        elif node.op=='mul':
            value=values[0]*values[1];unit=args[1].unit if unit=='one' else unit
        elif node.op in {'div','floordiv'}:
            if values[1]==0:raise MathIRRefusal('division by zero')
            value=values[0]/values[1] if node.op=='div' else Fraction(values[0]//values[1])
            unit='one' if args[1].unit!='one' else unit
        elif node.op=='neg':value=-values[0]
        elif node.op=='abs':value=abs(values[0])
        elif node.op=='floor':value=Fraction(values[0]//1)
        elif node.op=='sum':value=sum(values,Fraction(0))
        elif node.op=='mean':value=sum(values,Fraction(0))/len(values)
        elif node.op in {'percent','increase_percent','decrease_percent'}:
            rate=values[1]/100
            factor=rate if node.op=='percent' else 1+rate if node.op=='increase_percent' else 1-rate
            value=values[0]*factor
        elif node.op=='power':
            exponent=int(values[1]);base=values[0]
            if base==0 and exponent<=0:raise MathIRRefusal('undefined zero power')
            if abs(exponent)*max(base.numerator.bit_length(),base.denominator.bit_length())>MAX_BITS:
                raise MathIRRefusal('power growth limit exceeded')
            value=base**exponent
        elif node.op=='round':
            scale=10**int(values[1]);scaled=abs(values[0])*scale
            whole,remainder=divmod(scaled.numerator,scaled.denominator)
            whole+=2*remainder>=scaled.denominator
            value=Fraction(-whole if values[0]<0 else whole,scale)
        elif node.op=='convert':
            value=values[0]*UNITS[unit][1]/UNITS[node.option][1];unit=node.option
        else:raise MathIRRefusal('unhandled validated operation')
        if time.monotonic() >= deadline:raise MathIRRefusal('IR execution deadline reached')
        return Quantity(bounded(value),unit)
    return visit(program.root)


def serialize(quantity, program):
    """Use the established exact numeric convention, with no implicit rounding."""
    validate(program)
    if type(quantity) is not Quantity or quantity.unit != program.output_unit:
        raise MathIRRefusal('serialized result has wrong dimensions')
    return format_math(bounded(quantity.value))
