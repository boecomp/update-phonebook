#CSV import in Wildix PBX Telefonbuecher - die Konfig muss in config.py angepasst werden.
#
# WICHTIGER HINTERGRUND:
# Die Listen-API der PBX (GET .../phonebooks/{id}/contacts/) ignoriert bei diesem
# System "limit"/"offset"/"page"/"skip" komplett und liefert IMMER nur die ersten
# 100 Kontakte zurueck. Ein "alle Kontakte laden und live abgleichen"-Sync ist damit
# nicht zuverlaessig moeglich.
#
# LOESUNG: Das Telefonbuch wird VOR dem ersten Lauf manuell in WMS geleert (macht der
# Benutzer selbst). Danach:
#   1. Erstlauf (keine state-Datei vorhanden): alle CSV-Zeilen werden frisch angelegt,
#      jeweils mit document_id = CSV-Id. Der Zustand wird in einer lokalen Datei
#      (config.state_file_path) gespeichert: document_id -> {contact_id, Feldwerte}
#   2. Alle weiteren Laeufe: es wird NUR NOCH gegen diese lokale Datei verglichen,
#      die Listen-API wird nicht mehr gebraucht:
#        - CSV-Zeile mit bekannter document_id + unveraenderten Werten -> ueberspringen
#        - CSV-Zeile mit bekannter document_id + geaenderten Werten -> Update (PUT)
#        - CSV-Zeile mit neuer document_id -> neu anlegen (POST)
#        - document_id in der state-Datei, aber nicht mehr in der CSV -> loeschen (DELETE)
#
# Benoetigter Scope auf dem API Key: phonebooks:*  (oder pbx:*)

import csv
import json
import os
import re
import requests
import phonenumbers
import config
import urllib.parse
import sys
import logging
from time import sleep

try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

REQUEST_TIMEOUT = 20  # Sekunden

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('import_errors.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TRACKED_FIELDS = ['name', 'phone', 'mobile', 'email', 'organization', 'note']


# ---------- Validierung ----------

def is_valid_name(name):
    return bool(re.search(r'[A-Za-zÀ-ÿ]', name or ''))


def is_valid_email(email):
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


def to_e164(raw, region="CH"):
    raw = (raw or '').strip()
    if not raw:
        return ''
    try:
        parsed = phonenumbers.parse(raw, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return raw


def normalize_number(raw, region="CH"):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164).lstrip('+')
    except phonenumbers.NumberParseException:
        pass
    digits = re.sub(r'\D', '', raw)
    return digits if len(digits) >= 6 else None


# ---------- State-Datei ----------

def load_state():
    if not os.path.exists(config.state_file_path):
        return None
    with open(config.state_file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_state(state):
    tmp_path = config.state_file_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, config.state_file_path)  # atomarer Ersatz, kein halbgeschriebenes File bei Absturz


# ---------- HTTP Helpers ----------

def get_headers():
    return {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Authorization': f'Bearer {config.api_key}',
    }


def send_payload(fields):
    parts = [f'data%5B{k}%5D={urllib.parse.quote(str(v))}' for k, v in fields.items()]
    return '&'.join(parts)


def exit_program():
    log.info("Exiting the program...")
    sys.exit(0)


# ---------- CSV lesen + validieren ----------

def read_and_validate_csv(csv_file):
    """Liest die CSV, validiert jede Zeile und gibt eine Liste von dicts zurueck."""
    rows = []
    stats = {'valid': 0, 'invalid': 0}

    with open(csv_file, mode='r', encoding='utf-8-sig', newline='') as file:
        csv_reader = csv.DictReader(file)
        for line_no, row in enumerate(csv_reader, start=2):
            record_id = row.get('Id', '').strip()
            name = row.get('Name', '').strip()
            email = row.get('Email', '').strip()
            organization = row.get('Organization', '').strip()
            note = row.get('Abteilung', '').strip()

            if not record_id:
                log.warning(f'Zeile {line_no} uebersprungen: keine Id in der CSV vorhanden.')
                stats['invalid'] += 1
                continue

            if not is_valid_name(name):
                log.warning(f'Zeile {line_no} (Id {record_id}) uebersprungen: ungueltiger Name {name!r}')
                stats['invalid'] += 1
                continue

            if not is_valid_email(email):
                log.warning(f'Zeile {line_no} (Id {record_id}, {name}): ungueltige Email {email!r}, '
                            f'wird nicht importiert (Feld bleibt leer)')
                email = ''

            phone_plus = to_e164(row.get('Phone', ''))
            phone_norm = normalize_number(row.get('Phone', ''))
            mobile_norm = normalize_number(row.get('Mobile', ''))
            mobile_value = to_e164(row.get('Mobile', '')) if mobile_norm else row.get('Mobile', '').strip()

            if not phone_norm and not mobile_norm:
                log.warning(f'Zeile {line_no} (Id {record_id}, {name}) uebersprungen: '
                            f'weder Phone noch Mobile ist eine gueltige Nummer '
                            f'(Phone={row.get("Phone")!r}, Mobile={row.get("Mobile")!r})')
                stats['invalid'] += 1
                continue

            rows.append({
                'record_id': record_id,
                'name': name,
                'phone': phone_plus,
                'mobile': mobile_value,
                'email': email,
                'organization': organization,
                'note': note,
                'type': row.get('Type', '').strip(),
            })
            stats['valid'] += 1

    log.info(f'CSV eingelesen: {stats["valid"]} gueltige Zeilen, {stats["invalid"]} uebersprungen.')
    return rows


# ---------- Erstlauf: leeres Telefonbuch -> alle Kontakte anlegen ----------

def initial_import(session, api_url, headers, phonebook_id, rows):
    state = {}
    created, failed = 0, 0

    for i, row in enumerate(rows, start=1):
        if i % 200 == 0:
            log.info(f'  ... {i} von {len(rows)} importiert')

        payload_fields = dict(
            name=row['name'], phonebook_id=phonebook_id, phone=row['phone'],
            mobile=row['mobile'], email=row['email'], type=row['type'],
            organization=row['organization'], note=row['note'],
            document_id=row['record_id'],
        )
        data = send_payload(payload_fields)

        try:
            response = session.post(api_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.ConnectionError:
            log.warning('Connection Error: warte 10 sekunden')
            sleep(10)
            continue

        if response.status_code == 200:
            new_id = response.json().get('result', {}).get('id')
            state[row['record_id']] = {
                'contact_id': new_id,
                'name': row['name'], 'phone': row['phone'], 'mobile': row['mobile'],
                'email': row['email'], 'organization': row['organization'], 'note': row['note'],
            }
            created += 1
        else:
            log.error(f'Fehler beim Anlegen von Id {row["record_id"]} ({row["name"]}). '
                      f'Statuscode: {response.status_code} Antwort: {response.text}')
            failed += 1

        # Zwischenspeichern alle 200 Kontakte, damit bei einem Abbruch nicht alles verloren geht
        if i % 200 == 0:
            save_state(state)

    log.info(f'Erstimport fertig: {created} angelegt, {failed} fehlgeschlagen.')
    return state


# ---------- Folgelaeufe: state-basierter Sync ----------

def sync_with_state(session, api_url, phonebook_contacts_url, headers, phonebook_id, rows, state):
    seen_ids = set()
    stats = {'created': 0, 'updated': 0, 'unchanged': 0, 'failed': 0}

    for i, row in enumerate(rows, start=1):
        if i % 200 == 0:
            log.info(f'  ... {i} von {len(rows)} verarbeitet')

        rid = row['record_id']
        seen_ids.add(rid)
        existing = state.get(rid)

        payload_fields = dict(
            name=row['name'], phonebook_id=phonebook_id, phone=row['phone'],
            mobile=row['mobile'], email=row['email'], type=row['type'],
            organization=row['organization'], note=row['note'], document_id=rid,
        )

        if existing is None:
            data = send_payload(payload_fields)
            try:
                response = session.post(api_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.ConnectionError:
                log.warning('Connection Error: warte 10 sekunden')
                sleep(10)
                continue

            if response.status_code == 200:
                new_id = response.json().get('result', {}).get('id')
                state[rid] = {
                    'contact_id': new_id,
                    'name': row['name'], 'phone': row['phone'], 'mobile': row['mobile'],
                    'email': row['email'], 'organization': row['organization'], 'note': row['note'],
                }
                stats['created'] += 1
                log.info(f'Id {rid} ({row["name"]}): neu angelegt.')
            else:
                log.error(f'Fehler beim Anlegen von Id {rid} ({row["name"]}). '
                          f'Statuscode: {response.status_code} Antwort: {response.text}')
                stats['failed'] += 1
            continue

        changed_fields = [f for f in TRACKED_FIELDS if row[f] != existing.get(f, '')]
        if not changed_fields:
            stats['unchanged'] += 1
            continue

        put_url = f'{phonebook_contacts_url}{existing["contact_id"]}/'
        data = send_payload(payload_fields)
        try:
            response = session.put(put_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.ConnectionError:
            log.warning('Connection Error: warte 10 sekunden')
            sleep(10)
            continue

        if response.status_code == 200:
            state[rid] = {
                'contact_id': existing['contact_id'],
                'name': row['name'], 'phone': row['phone'], 'mobile': row['mobile'],
                'email': row['email'], 'organization': row['organization'], 'note': row['note'],
            }
            stats['updated'] += 1
            log.info(f'Id {rid} ({row["name"]}): aktualisiert, geaenderte Felder: {changed_fields}')
        else:
            log.error(f'Fehler beim Update von Id {rid} ({row["name"]}). '
                      f'Statuscode: {response.status_code} Antwort: {response.text}')
            stats['failed'] += 1

    # Kontakte loeschen, die es in der state-Datei gibt, aber nicht mehr in der CSV
    orphaned_ids = [rid for rid in state if rid not in seen_ids]
    log.info(f'{len(orphaned_ids)} Kontakte nicht mehr in der CSV -> werden geloescht.')
    for rid in orphaned_ids:
        contact_id = state[rid]['contact_id']
        del_url = f'{phonebook_contacts_url}{contact_id}/'
        try:
            del_response = session.delete(del_url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            log.error(f'Fehler beim Loeschen von Kontakt {contact_id} (Id {rid}): {e}')
            continue

        if del_response.status_code == 200:
            log.info(f'Id {rid}: Kontakt {contact_id} geloescht (nicht mehr in CSV).')
            del state[rid]
        else:
            log.error(f'Fehler beim Loeschen von Kontakt {contact_id} (Id {rid}). '
                      f'Statuscode: {del_response.status_code} Antwort: {del_response.text}')

    log.info(f"Sync fertig: {stats['created']} neu, {stats['updated']} aktualisiert, "
             f"{stats['unchanged']} unveraendert, {len(orphaned_ids)} geloescht, "
             f"{stats['failed']} fehlgeschlagen.")
    return state


# ---------- Main ----------

if __name__ == "__main__":
    log.info("=== Wildix Contact Sync gestartet ===")
    log.info(f"Phonebook-URL: {config.phonebook_contacts_url}")

    headers = {'Authorization': f'Bearer {config.api_key}'}
    state = load_state()

    with requests.Session() as session:
        session.headers.update(headers)

        if state is None:
            log.info(f'Keine state-Datei gefunden ({config.state_file_path}) -> ERSTLAUF-MODUS: '
                     'alle CSV-Zeilen werden neu angelegt (Telefonbuch sollte bereits leer sein).')
            rows = read_and_validate_csv(config.csv_file_path)
            state = initial_import(session, config.api_url, headers, config.phonebook_id, rows)
            save_state(state)
            log.info(f'Erstimport abgeschlossen. Zustand gespeichert in {config.state_file_path}.')
        else:
            log.info(f'state-Datei gefunden ({len(state)} bekannte Kontakte) -> normaler Sync-Modus.')
            rows = read_and_validate_csv(config.csv_file_path)
            state = sync_with_state(
                session, config.api_url, config.phonebook_contacts_url, headers,
                config.phonebook_id, rows, state
            )
            save_state(state)
            log.info(f'Sync abgeschlossen. Zustand gespeichert in {config.state_file_path}.')