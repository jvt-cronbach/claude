#!/usr/bin/env python3
"""Bettet data/themenwelten.json in themenwelten.html ein (zwischen /*DATA-START*/ und /*DATA-END*/).
Aufruf: python3 tools/inline-data.py [pfad/zur/daten.json]
Prüft vorher die Struktur: mindestens eine Welt, je Inhalt ein Name und ein Profil aus neun Zahlen."""
import json, re, sys, pathlib

root = pathlib.Path(__file__).resolve().parent.parent
src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'data' / 'themenwelten.json'
page = root / 'themenwelten.html'


def fail(msg):
    print('Fehler:', msg, file=sys.stderr)
    sys.exit(1)


def check_profile(profile, where):
    if not (isinstance(profile, list) and len(profile) == 9):
        fail(f'{where}: profile muss eine Liste aus 9 Zahlen sein')
    for v in profile:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 1):
            fail(f'{where}: profile-Werte müssen Zahlen zwischen 0 und 1 sein')


try:
    data = json.loads(src.read_text(encoding='utf-8'))
except (OSError, ValueError) as e:
    fail(f'{src} konnte nicht gelesen werden: {e}')

product = data.get('product') if isinstance(data, dict) else None
if not isinstance(product, dict) or not isinstance(product.get('name'), str) or not product['name'].strip():
    fail('product.name fehlt')
check_profile(product.get('profile'), 'product')
worlds = data.get('worlds')
if not isinstance(worlds, list) or not worlds:
    fail('worlds muss mindestens eine Welt enthalten')
for k, w in enumerate(worlds):
    where = f'worlds[{k}]'
    for key in ('id', 'name', 'noun'):
        if not isinstance(w.get(key), str) or not w[key].strip():
            fail(f'{where}.{key} fehlt')
    items = w.get('items')
    if not isinstance(items, list) or not items:
        fail(f'{where}.items muss mindestens einen Inhalt enthalten')
    for j, it in enumerate(items):
        if not isinstance(it.get('name'), str) or not it['name'].strip():
            fail(f'{where}.items[{j}].name fehlt')
        check_profile(it.get('profile'), f'{where}.items[{j}] ({it.get("name")})')

# Heatmaps des Psychografie-Stapels: "imageFile" (Pfad unter assets/) wird als Data-URL eingebettet
import base64, mimetypes
for layer in (data.get('psychografie') or {}).get('layers', []):
    f = layer.get('imageFile')
    if not f:
        continue
    fp = (root / f).resolve()
    if not str(fp).startswith(str((root / 'assets').resolve())) or not fp.is_file():
        fail(f'imageFile {f}: Datei muss unter assets/ liegen und existieren')
    mime = mimetypes.guess_type(fp.name)[0] or 'image/png'
    layer['image'] = 'data:' + mime + ';base64,' + base64.b64encode(fp.read_bytes()).decode('ascii')

html = page.read_text(encoding='utf-8')
# "<" wird als \u003c geschrieben: gültiges JSON und JavaScript, und kein "</script" oder "<!--" im Skriptblock
payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
block = '/*DATA-START*/\nvar THEMENWELTEN_DATA = ' + payload + ';\n/*DATA-END*/'
new, n = re.subn(r'^/\*DATA-START\*/\n.*?\n/\*DATA-END\*/$', lambda m: block, html, count=1, flags=re.S | re.M)
if n != 1:
    fail('Datenblock in themenwelten.html nicht gefunden')
page.write_text(new, encoding='utf-8')
print('eingebettet:', src, '->', page, f'({len(worlds)} Welten, {sum(len(w["items"]) for w in worlds)} Inhalte)')
