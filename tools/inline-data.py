#!/usr/bin/env python3
"""Bettet data/themenwelten.json in themenwelten.html ein (zwischen /*DATA-START*/ und /*DATA-END*/).
Aufruf: python3 tools/inline-data.py [pfad/zur/daten.json]"""
import json, re, sys, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'data' / 'themenwelten.json'
page = root / 'themenwelten.html'
data = json.loads(src.read_text(encoding='utf-8'))
html = page.read_text(encoding='utf-8')
block = '/*DATA-START*/\nvar THEMENWELTEN_DATA = ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/') + ';\n/*DATA-END*/'
new, n = re.subn(r'/\*DATA-START\*/.*?/\*DATA-END\*/', lambda m: block, html, flags=re.S)
assert n == 1, 'Datenblock nicht gefunden'
page.write_text(new, encoding='utf-8')
print('eingebettet:', src, '->', page, f'({len(data["worlds"])} Welten)')
