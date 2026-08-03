#CSV import in Wildix PBX Telefonbuecher - die Konfig muss in config.py angepasst werden.
#
# Sync-Logik (Company API Key Authentifizierung):
#
#   1. Alle bestehenden Kontakte im Telefonbuch abrufen (Pagination, dedupliziert nach Id)
#   2. Matching-Key ist die CSV-Id, gespeichert im Kontaktfeld "document_id":
#        - Kontakt mit passender document_id gefunden -> Felder vergleichen,
#          nur bei tatsaechlicher Aenderung ein Update (PUT) schicken, sonst ueberspringen
#        - Kein Treffer per document_id -> Fallback: alten Kontakt (noch OHNE document_id)
#          per Telefonnummer suchen und "adoptieren" (document_id nachtraeglich setzen).
#          Das ist die einmalige Migration von Alt-Kontakten, die vor Einfuehrung
#          dieses Schemas angelegt wurden.
#        - Auch das schlaegt fehl -> neuer Kontakt wird angelegt (POST), inkl. document_id
#   3. Kontakte, die ein document_id besitzen, aber in der aktuellen CSV nicht mehr
#      vorkommen, werden geloescht (verwaiste, von diesem Script verwaltete Kontakte).
#      Kontakte OHNE document_id (z.B. manuell in WMS angelegt) werden NIE automatisch
#      geloescht, auch wenn sie nicht in der CSV stehen.
#
# Benoetigter Scope auf dem API Key: phonebooks:*  (oder pbx:*)

import csv
import re
import requests
import phonenumbers
import config
import urllib.parse
import sys
import logging
from time import sleep

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


def is_valid_name(name):
    return bool(re.search(r'[A-Za-zÀ-ÿ]', name or ''))


def is_valid_email(email):
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


def normalize_number(raw, region="CH"):
    """Normalisiert eine Telefonnummer auf E.164 ohne '+' (nur Ziffern).
    None, wenn nichts Sinnvolles extrahiert werden kann."""
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


def to_e164(raw, region="CH"):
    """Wie normalize_number, aber MIT '+' fuer den direkten Feldvergleich/Versand.
    Gibt den Original-String zurueck, falls kein gueltiges Parsing moeglich ist."""
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


def get_headers():
    return {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Authorization': f'Bearer {config.api_key}',
    }


def get_all_contacts(phonebook_contacts_url, headers, page_size=100):
    """Alle Kontakte eines Telefonbuchs abrufen, dedupliziert nach Id."""
    contacts_by_id = {}
    offset = 0

    while True:
        response = requests.get(
            phonebook_contacts_url,
            headers=headers,
            params={'limit': page_size, 'offset': offset}
        )

        if response.status_code != 200:
            log.error(f'Fehler beim Abrufen der Kontakte (offset {offset}). '
                      f'Statuscode: {response.status_code} Antwort: {response.text}')
            exit_program()

        data = response.json()
        result = data.get('result', data)
        records = result.get('records', []) if isinstance(result, dict) else result
        total = result.get('total', len(records)) if isinstance(result, dict) else len(records)

        if not records:
            break

        for record in records:
            cid = record.get('id')
            if cid is not None:
                contacts_by_id[cid] = record

        if len(contacts_by_id) >= total or len(records) < page_size:
            break
        offset += page_size

    return contacts_by_id


def build_document_id_index(contacts_by_id):
    """document_id (CSV-Id) -> Kontakt-Id, nur fuer Kontakte, die bereits eine document_id haben."""
    index = {}
    for cid, contact in contacts_by_id.items():
        doc_id = (contact.get('document_id') or '').strip()
        if doc_id:
            index[doc_id] = cid
    return index


def build_legacy_phone_index(contacts_by_id):
    """Telefonnummer -> Kontakt-Id, NUR fuer Kontakte OHNE document_id (Migrations-Fallback)."""
    index = {}
    for cid, contact in contacts_by_id.items():
        if (contact.get('document_id') or '').strip():
            continue  # hat schon eine document_id, gehoert nicht in den Fallback-Index
        for field in ('phone', 'mobile'):
            norm = normalize_number(contact.get(field, ''))
            if norm:
                index.setdefault(norm, cid)
    return index


def diff_contact(existing, name, phone_plus, mobile, email, organization, note, record_id):
    """Vergleicht bestehenden Kontakt mit den CSV-Werten. Gibt dict der geaenderten
    Felder zurueck (leer = keine Aenderung)."""
    changes = {}
    checks = {
        'name': name,
        'phone': phone_plus,
        'mobile': mobile,
        'email': email,
        'organization': organization,
        'note': note,
        'document_id': record_id,
    }
    for field, new_value in checks.items():
        old_value = (existing.get(field) or '').strip()
        if (new_value or '').strip() != old_value:
            changes[field] = new_value
    return changes


def delete_contacts(phonebook_contacts_url, headers, contact_ids):
    if not contact_ids:
        log.info('Keine verwaisten (von diesem Script verwalteten) Kontakte zu loeschen.')
        return

    log.info(f'{len(contact_ids)} verwaiste Kontakte (document_id gesetzt, aber nicht mehr in CSV) '
             f'werden geloescht...')

    for contact_id in contact_ids:
        del_url = f'{phonebook_contacts_url}{contact_id}/'
        del_response = requests.delete(del_url, headers=headers)

        if del_response.status_code == 200:
            log.info(f'Kontakt {contact_id} erfolgreich geloescht.')
        else:
            log.error(f'Fehler beim Loeschen von Kontakt {contact_id}. '
                      f'Statuscode: {del_response.status_code} Antwort: {del_response.text}')


def sync_contacts_from_csv(api_url, phonebook_contacts_url, csv_file, phonebook_id,
                            existing_contacts, doc_id_index, legacy_phone_index):
    headers = get_headers()
    matched_ids = set()
    stats = {'created': 0, 'updated': 0, 'unchanged': 0, 'adopted': 0, 'invalid': 0}

    with open(csv_file, mode='r', encoding='utf-8-sig', newline='') as file:
        csv_reader = csv.DictReader(file)
        for line_no, row in enumerate(csv_reader, start=2):
            record_id = row.get('Id', '').strip()
            name = row.get('Name', '').strip()
            email = row.get('Email', '').strip()
            organization = row.get('Organization', '').strip()
            note = row.get('Abteilung', '').strip()
            row_type = row.get('Type', '').strip()

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

            payload_fields = dict(
                name=name, phonebook_id=phonebook_id, phone=phone_plus, mobile=mobile_value,
                email=email, type=row_type, organization=organization, note=note,
                document_id=record_id,
            )

            def send_payload(fields):
                parts = [f'data%5B{k}%5D={urllib.parse.quote(str(v))}' for k, v in fields.items()]
                return '&'.join(parts)

            # 1. Primaer: Matching per document_id (CSV-Id)
            existing_id = doc_id_index.get(record_id)
            adopted = False

            # 2. Fallback: Migration alter Kontakte (noch ohne document_id) per Telefonnummer
            if existing_id is None:
                existing_id = legacy_phone_index.get(phone_norm) or legacy_phone_index.get(mobile_norm)
                adopted = existing_id is not None

            if existing_id is not None:
                matched_ids.add(existing_id)
                existing = existing_contacts.get(existing_id, {})
                changes = diff_contact(existing, name, phone_plus, mobile_value, email,
                                        organization, note, record_id)

                if adopted or changes:
                    put_url = f'{phonebook_contacts_url}{existing_id}/'
                    data = send_payload(payload_fields)
                    try:
                        response = requests.put(put_url, headers=headers, data=data)
                    except requests.exceptions.ConnectionError:
                        log.warning('Connection Error: warte 10 sekunden')
                        sleep(10)
                        continue

                    if response.status_code == 200:
                        if adopted:
                            log.info(f'Zeile {line_no} (Id {record_id}, {name}): alter Kontakt '
                                     f'{existing_id} per Telefonnummer adoptiert, document_id gesetzt.')
                            stats['adopted'] += 1
                        else:
                            log.info(f'Zeile {line_no} (Id {record_id}, {name}): Kontakt {existing_id} '
                                     f'aktualisiert, geaenderte Felder: {list(changes.keys())}')
                            stats['updated'] += 1
                    else:
                        log.error(f'Zeile {line_no} (Id {record_id}, {name}): Fehler beim Update '
                                  f'von Kontakt {existing_id}. Statuscode: {response.status_code} '
                                  f'Antwort: {response.text}')
                else:
                    log.debug(f'Zeile {line_no} (Id {record_id}, {name}): unveraendert, uebersprungen.')
                    stats['unchanged'] += 1
                continue

            # 3. Kein Treffer -> neuer Kontakt
            data = send_payload(payload_fields)
            try:
                response = requests.post(api_url, headers=headers, data=data)
            except requests.exceptions.ConnectionError:
                log.warning('Connection Error: warte 10 sekunden')
                sleep(10)
                continue

            if response.status_code == 200:
                log.info(f'Zeile {line_no} (Id {record_id}, {name}): neu importiert.')
                stats['created'] += 1
                new_id = response.json().get('result', {}).get('id')
                if new_id is not None:
                    matched_ids.add(new_id)
            else:
                log.error(f'Zeile {line_no} (Id {record_id}, {name}): Fehler beim Anlegen. '
                          f'Statuscode: {response.status_code} Antwort: {response.text}')

    log.info(f"CSV-Sync fertig: {stats['created']} neu angelegt, {stats['updated']} aktualisiert, "
             f"{stats['adopted']} alte Kontakte adoptiert, {stats['unchanged']} unveraendert, "
             f"{stats['invalid']} ungueltige Zeilen uebersprungen.")

    return matched_ids


def exit_program():
    log.info("Exiting the program...")
    sys.exit(0)


if __name__ == "__main__":
    headers = {'Authorization': f'Bearer {config.api_key}'}

    # 1. Bestehende Kontakte laden
    existing_contacts = get_all_contacts(config.phonebook_contacts_url, headers)
    log.info(f'{len(existing_contacts)} bestehende Kontakte im Telefonbuch gefunden.')

    doc_id_index = build_document_id_index(existing_contacts)
    legacy_phone_index = build_legacy_phone_index(existing_contacts)
    log.info(f'{len(doc_id_index)} Kontakte mit document_id (von diesem Script verwaltet), '
             f'{len(legacy_phone_index)} Telefonnummern aus Alt-Kontakten fuer Migration verfuegbar.')

    # 2. CSV syncen
    matched_ids = sync_contacts_from_csv(
        config.api_url, config.phonebook_contacts_url, config.csv_file_path,
        config.phonebook_id, existing_contacts, doc_id_index, legacy_phone_index
    )

    # 3. Verwaiste, von diesem Script verwaltete Kontakte loeschen
    #    (nur Kontakte mit document_id, die NICHT in der CSV vorkamen)
    managed_ids = set(doc_id_index.values())
    orphaned_ids = [cid for cid in managed_ids if cid not in matched_ids]
    delete_contacts(config.phonebook_contacts_url, headers, orphaned_ids)