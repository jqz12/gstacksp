"""Structured Cargo test target summaries, without overriding process status."""
import re


def cargo_summary(output):
    rows=re.findall(r'^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; \d+ measured; (\d+) filtered out;(?:.*)$',output,re.M)
    if not rows:return None
    totals=[sum(int(row[i]) for row in rows) for i in range(4)]
    return dict(zip(('framework','targets','passed','failed','ignored','filtered'),('cargo',len(rows),*totals)))
