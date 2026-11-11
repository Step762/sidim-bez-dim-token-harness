"""Independent qualification controls: fixed oracles, isolated KB/CSV, no network."""
import argparse
import asyncio
from contextlib import redirect_stdout
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from budget_manager import BudgetManager
import harness


def case(key, kind, question, expected, property, oracle, *, files=None, required=True):
    return dict(id=key, task_type=kind, instruction=question, expected=expected,
                property=property, oracle=oracle, files=files or {}, required=required)


def kb(*records):
    return {f'kb/document_{i * 7 + 3}.md': record for i, record in enumerate(records)}


def csv_case(key, question, table, expected, property, oracle, *, required=True):
    return case(key, 'data_analysis', question, expected, property, oracle,
                files={'observations.csv': table}, required=required)


# Oracles are written before running the solver. No benchmark IDs or holdout.
CASES = [
    case('s_alias_chain', 'search', "Which city is Kite's adviser's firm based in?", 'Lübeck',
         'Explicit alias and two directed links across unrelated filenames.',
         'Kite -> Nia -> Omri -> Reed Labs -> Lübeck, by the three explicit edges.',
         files=kb('# Nia\nAlias: Kite\nMentor: Omri\n', '# Omri\nEmployer: Reed Labs\n',
                  '# Reed Labs\nHeadquarters: Lübeck\n', '# Noise\nHeadquarters: Quito\n')),
    case('s_three_links', 'search', 'Where is the employer of the mentor of the mentor of Veda located?', 'Nuuk',
         'Three links with OF nesting and a distractor branch.',
         'Veda -> Ivo -> Neve -> Snowline -> Nuuk; distractor employer not on path.',
         files=kb('# Veda\nMentor: Ivo\nEmployer: Decoy\n# Ivo\nMentor: Neve\n',
                  '# Neve\nEmployer: Snowline\n# Snowline\nHeadquarters: Nuuk\n# Decoy\nHeadquarters: Rome\n')),
    case('s_birth_prose', 'search', "What is the birth year of Rumi's coach?", '1937',
         'Subject-bound prose at end of a directed chain; other birth year is noise.',
         'Rumi.Mentor=Tenzin; Tenzin was born in 1937.',
         files=kb('# Rumi\nMentor: Tenzin\nBorn: 1991\n', '# Tenzin\nTenzin was born in 1937 and later moved to Oslo.\n')),
    case('s_inverse_noise', 'search', 'Who is mentored by Oren?', 'Zuri',
         'Unrelated conflicting birth records must not hide a unique inverse Mentor.',
         'Only Zuri has Mentor Oren; the conflicting archive has no Mentor fact.',
         files=kb('# Zuri\nMentor: Oren\n# Oren\nBorn: 1978\n',
                  '# Archive\nBorn: 1922\n', '# Archive\nBorn: 1923\n')),
    case('s_inverse_field_noise', 'search', 'Who is mentored by Oren?', 'Zuri',
         'Contradictory unrelated headquarters in one record is not Mentor evidence.',
         'Only Zuri has Mentor Oren; neither city belongs to the query path.',
         files=kb('# Zuri\nMentor: Oren\n# Oren\nBorn: 1978\n',
                  '# Archive\nHeadquarters: Quito\nHeadquarters: Riga\n')),
    case('s_conflicting_edge', 'search', "Where is Nia's employer based?", None,
         'Conflicting outgoing edges forbid choosing a convenient city.',
         'Both employers are explicit and disagree; no unique answer.',
         files=kb('# Nia\nEmployer: Reed\nEmployer: Pine\n# Reed\nHeadquarters: Riga\n# Pine\nHeadquarters: Oslo\n')),
    case('s_alias_collision', 'search', 'When was Kite born?', None,
         'Shared alias does not prove identity.', 'Kite refers to two different headings.',
         files=kb('# Nia\nAlias: Kite\nBorn: 1981\n# Neve\nAlias: Kite\nBorn: 1982\n')),
    case('s_inverse_lost_edge', 'search', 'Who is mentored by Oren?', None,
         'A conflicting earlier Mentor record cannot be hidden by a later record.',
         'Both Zuri and possibly Veda have Mentor Oren; Veda also has conflicting Mentor Ivo.',
         files=kb('# Zuri\nMentor: Oren\n# Oren\nBorn: 1978\n# Ivo\nBorn: 1980\n',
                  '# Veda\nMentor: Oren\n', '# Veda\nMentor: Ivo\n')),
    case('s_quoted_noise', 'search', 'When was Neve born?', '1942',
         'Fenced and quoted pseudo-facts are inert.', 'Only the unquoted explicit Born field is evidence.',
         files=kb('# Neve\nBorn: 1942\n> Born: 1900\n```text\n# Neve\nBorn: 1901\n```\n')),
    case('s_missing_target', 'search', "Where is Neve's firm based?", None,
         'Absent endpoint cannot be inferred from a similarly named company.', 'Missing Works has no profile.',
         files=kb('# Neve\nEmployer: Missing Works\n# Other Works\nHeadquarters: Bonn\n')),
    case('s_inverse_missing', 'search', 'Who is mentored by Oren?', None,
         'Unknown Mentor could add another inverse candidate.', 'Veda has unknown Mentor; uniqueness is unproved.',
         files=kb('# Oren\nBorn: 1978\n# Zuri\nMentor: Oren\n# Veda\nMentor: unknown\n')),
    case('s_inverse_employer_boundary', 'search', 'Who works for Reed Labs?', 'Nia',
         'Coverage boundary: inverse Employer has a unique answer but no supported route.',
         'One Employer edge to Reed Labs; not an inverse Mentor query.',
         files=kb('# Nia\nEmployer: Reed Labs\n# Reed Labs\nHeadquarters: Lübeck\n'), required=False),
    case('m_precedence', 'math', 'Evaluate -3.25 + 2 * (7 - 1.5).', str(Fraction(-13,4)+2*(7-Fraction(3,2))),
         'Negative decimal, parentheses and conventional precedence.', 'Exact Fraction arithmetic, -13/4 + 2*(7-3/2).'),
    case('m_ltr', 'math', 'Evaluate -3.25 + 2 * 4 from left to right.', '-5',
         'Explicit LTR differs from standard precedence.', '(-3.25+2)*4 = -1.25*4 = -5.'),
    case('m_steps_percent', 'math', 'Start with 80 units. Increase by 25 percent. Subtract 4 units. Divide by 3.', '32',
         'Ordered relative percentage then subtraction then division.', '(80*1.25 - 4)/3 = 32.'),
    case('m_floor_negative', 'math', 'Start with -17 points. Divide by 3 rounded down.', '-6',
         'Floor is not truncation toward zero.', '-17/3 lies strictly between -6 and -5; floor is -6.'),
    case('m_half_up', 'math', 'Round -12.345 to two decimal places using round-half-up.',
         str(Decimal('-12.345').quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
         'Negative tie rounds away from zero.', 'Independent decimal.ROUND_HALF_UP.'),
    case('m_unit_currency', 'math', 'Start with 12 dollars. Add 3 euros.', None,
         'Incompatible currencies need an exchange rule.', 'No exchange rate; 15 has no determined unit.'),
    case('m_unit_dimensions', 'math', 'Start with 6 meters. Add 4 litres.', None,
         'Different physical dimensions cannot be added.', 'Length plus volume has no scalar total.'),
    case('m_unit_binary', 'math', 'Subtract 2 dollars from 9 euros.', None,
         'Operand units cannot be dropped in the initial binary clause.', 'No exchange rate determines the difference.'),
    case('m_unit_plural', 'math', 'Start with 1 box. Add 6 boxes. Divide by 2.', '3.5',
         'Singular and plural denote the same unit.', '(1+6)/2 = 3.5 boxes.'),
    case('m_zero_division', 'math', 'Evaluate (3 - 3) / (2 - 2).', None,
         'Zero numerator does not make division by zero valid.', 'Denominator is exactly zero.'),
    case('m_conditional', 'math', 'Start with 9 coins. Add 2 coins if the flag is true.', None,
         'Missing condition is not assumed true.', 'Flag is not given; result could be 9 or 11.'),
    case('m_scientific_boundary', 'math', 'Evaluate 1e2 + 0.5.', '100.5',
         'Coverage boundary: explicit scientific notation is not currently supported.',
         '10**2 + 1/2 = 100.5 by definition of base-10 exponent.', required=False),
    csv_case('d_renamed_range', "The file observations.csv has columns period,load,part. What is the sum of load where part = 'clamp' and period between 8 and 10 inclusive?",
             'period,load,part\n7,100,clamp\n8,-2.5,clamp\n9,.5,clamp\n10,+6,clamp\n11,100,clamp\n9,90,other\n', '4',
             'Renamed columns, exact decimals and both inclusive endpoints.', '-2.5+.5+6=4; out-of-range and other part excluded.'),
    csv_case('d_quoted_delimiter', 'The file observations.csv. What is the sum of amount where item = \'nut, "large"\'?',
             'item,amount\n"nut, ""large""",4\n"nut, ""large""",7\nother,90\n', '11',
             'CSV quoted delimiters and doubled quotes must preserve exact category.', 'Only the two matching rows, 4+7=11.'),
    csv_case('d_duplicate_count', 'The file observations.csv. How many rows are there where zone = \'west\'?',
             'zone,amount\nwest,3\nwest,3\neast,9\n', '2',
             'Identical records are rows, not an implicit DISTINCT.', 'Two west rows, counted separately.'),
    csv_case('d_group_mean', 'Using observations.csv, return a JSON object mapping each depot to its average of mass.',
             'depot,mass\nblue,1.5\nred,-2\nblue,2.5\nred,4\n', '{"blue":2,"red":1}',
             'Exact grouped means with independent state.', 'blue=(1.5+2.5)/2=2; red=(-2+4)/2=1.'),
    csv_case('d_rank_numeric', 'Using observations.csv, sort by rating descending and code lexicographically ascending to break ties. Return the result of the first row.',
             'rating,code,result\n9,z,low\n10,b,other\n10,a,chosen\n', 'chosen',
             'Numeric primary key, lexical secondary key and separate output column.', '10 > 9; within 10, a < b; selected result is chosen.'),
    csv_case('d_rank_conflict', 'Using observations.csv, sort by rating descending and code lexicographically ascending to break ties. Return the result of the first row.',
             'rating,code,result\n10,a,one\n10,a,two\n9,z,low\n', None,
             'Full sort tie with different output is ambiguous.', 'Two equally first rows have different outputs.'),
    csv_case('d_rank_same_output', 'Using observations.csv, sort by rating descending and code lexicographically ascending to break ties. Return the result of the first row.',
             'rating,code,result\n10,a,same\n10,a,same\n9,z,low\n', 'same',
             'Duplicates with identical output do not create answer ambiguity.', 'Every first row has result same.'),
    csv_case('d_decimal_comma', 'The file observations.csv. What is the sum of amount?',
             'amount\n"1,5"\n2\n', None,
             'Unspecified decimal locale must not be guessed.', '1,5 may be decimal or grouping notation; no declared locale.'),
    csv_case('d_empty_avg', 'The file observations.csv. What is the average of amount where zone = \'absent\'?',
             'zone,amount\nwest,3\n', None,
             'Mean of an empty selection is undefined.', 'No selected rows, divisor zero.'),
    csv_case('d_malformed_width', 'The file observations.csv. What is the sum of amount?',
             'name,amount\na,4\nb,5,extra\n', None,
             'A later damaged row invalidates the entire aggregate.', 'Third row has 3 fields for 2 headers.'),
    csv_case('d_duplicate_header', 'The file observations.csv. What is the sum of amount?',
             'amount,amount\n2,9\n', None,
             'Duplicate requested header has no unique column binding.', 'amount denotes two different columns.'),
    csv_case('d_boolean_boundary', "The file observations.csv. What is the sum of amount where (zone = 'west' and year = 2024) or zone = 'east'?",
             'zone,year,amount\nwest,2024,4\nwest,2023,8\neast,2023,2\n', '6',
             'Coverage boundary: explicitly grouped AND/OR is unambiguous but unsupported.',
             'The written parentheses select (west AND 2024) OR east, giving 4+2=6.', required=False),
]


def normalized(value):
    try:
        return str(Fraction(value))
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        return value


def run_probe():
    with tempfile.TemporaryDirectory(prefix='q45-stress-') as temporary:
        root=Path(temporary)
        for item in CASES:
            task=root/item['id'];task.mkdir()
            (task/'task.json').write_text(json.dumps({k:item[k] for k in ('task_type','instruction')} | {'reward':1}))
            for relative,text in item['files'].items():
                path=task/'workspace'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
        manager=BudgetManager(0);trace={}
        with patch.dict(os.environ,{},clear=True), patch('socket.socket.connect',side_effect=AssertionError('network forbidden')), redirect_stdout(io.StringIO()):
            counts=asyncio.run(harness.run_tasks(root,manager=manager,report=trace))
        outcomes={Path(key).name: value for key,value in trace['outcomes'].items()}
        rows=[]
        for item in CASES:
            path=root/item['id']/'answer.txt'
            actual=path.read_text().strip() if path.exists() else None
            correct=item['expected'] is not None and actual is not None and normalized(actual)==normalized(item['expected'])
            refused=actual is None and item['expected'] is None
            status='correct' if correct else 'safe_refusal' if refused else 'missed' if actual is None else 'wrong'
            rows.append({k:item[k] for k in ('id','task_type','instruction','expected','required','property','oracle')} | {
                'actual':actual,'status':status,'outcome':outcomes[item['id']],
                'answer_mode':(path.stat().st_mode & 0o777) if path.exists() else None})
        return {'scope':'independent synthetic scenarios; no benchmark or holdout inputs', 'counts':counts,
                'rows':rows,'summary':{key:sum(row['status']==key for row in rows) for key in ('correct','safe_refusal','missed','wrong')},
                'llm_requests':len(manager.snapshot()['requests']), 'spent':str(manager.snapshot()['spent'])}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run_probe()
    with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps(result['summary']))
