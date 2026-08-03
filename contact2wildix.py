#CSV import in Wildix PBX Telefonbuecher - die Konfig muss in config.py angepasst werden.
#
# Umgestellt auf die neue Company API Key Authentifizierung (Bearer Token).
# Benoetigter Scope auf dem API Key: phonebooks:*  (oder pbx:*)
#
# Aenderungen gegenueber der alten Version:
#   - kein Session-Cookie-Handling mehr noetig, nur noch "Authorization: Bearer <key>"
#   - Endpunkte jetzt kleingeschrieben (/api/v1/contacts/, /api/v1/phonebooks/{id}/contacts/)
#   - Loeschen erfolgt jetzt pro Kontakt einzeln (die neue API kennt kein
#     "loesche alle Kontakte im Telefonbuch" mehr), dafuer werden zuerst alle
#     Kontakt-IDs im Telefonbuch abgefragt und dann einzeln per DELETE entfernt.

import csv
import re
import requests
import phonenumbers
import config
import urllib.parse
import datetime
import sys
import logging
from time import sleep

# Logging-Setup: schreibt in Konsole UND in eine Log-Datei
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
    """Ein Name gilt nur als gueltig, wenn er mind. einen Buchstaben enthaelt."""
    return bool(re.search(r'[A-Za-zÀ-ÿ]', name or ''))


def is_valid_email(email):
    """Leere Email ist ok (kein Fehler), nur ein vorhandener, aber falsch formatierter
    Wert gilt als ungueltig."""
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


def get_headers():
    return {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Authorization': f'Bearer {config.api_key}',
    }


# Funktion zum Loeschen aller Kontakte in einem Telefonbuch
def del_contacts(phonebook_contacts_url):
    headers = {
        'Authorization': f'Bearer {config.api_key}',
    }

    # 1. Alle Kontakte im Telefonbuch abfragen, um die IDs zu bekommen
    response = requests.get(phonebook_contacts_url, headers=headers)
    current_time = datetime.datetime.now()

    if response.status_code != 200:
        log.error(f'Fehler beim Abrufen der Kontakte. Statuscode: {response.status_code} '
              f'Antwort: {response.text} Timestamp: {current_time}')
        exit_program()

    data = response.json()
    # Je nach API-Response-Format liegt die Liste direkt in "result" oder in "result.contacts" -
    # hier beide Faelle abfangen.
    contacts = data.get('result', data)
    if isinstance(contacts, dict):
        contacts = contacts.get('contacts', [])

    if not contacts:
        log.info('Keine Kontakte im Telefonbuch gefunden.')
        return

    log.info(f'{len(contacts)} Kontakte gefunden, werden geloescht...')

    # 2. Jeden Kontakt einzeln loeschen
    for contact in contacts:
        contact_id = contact.get('id')
        if contact_id is None:
            continue

        del_url = f'{phonebook_contacts_url}{contact_id}/'
        del_response = requests.delete(del_url, headers=headers)
        current_time = datetime.datetime.now()

        if del_response.status_code == 200:
            log.info(f'Kontakt {contact_id} erfolgreich geloescht.')
        else:
            log.error(f'Fehler beim Loeschen von Kontakt {contact_id}. '
                      f'Statuscode: {del_response.status_code} Antwort: {del_response.text}')


# Funktion zum Pruefen und Senden der Daten an die REST-API
def send_data_to_api(api_url, csv_file, phonebook_id):
    headers = get_headers()

    # Lese die CSV-Datei
    # utf-8-sig statt utf-8, damit ein evtl. BOM am Dateianfang nicht das erste
    # Spalten-Feld (Id) kaputt macht
    with open(csv_file, mode='r', encoding='utf-8-sig', newline='') as file:
        csv_reader = csv.DictReader(file)
        for line_no, row in enumerate(csv_reader, start=2):  # start=2: Zeile 1 = Header
            record_id = row.get('Id', '').strip()
            name = row.get('Name', '').strip()
            email = row.get('Email', '').strip()

            # Ungueltiger/leerer Name -> Datensatz komplett ueberspringen
            if not is_valid_name(name):
                log.warning(
                    f'Zeile {line_no} (Id {record_id}) uebersprungen: '
                    f'ungueltiger Name {name!r}'
                )
                continue

            # Ungueltige Email -> Kontakt trotzdem importieren, aber ohne Email
            if not is_valid_email(email):
                log.warning(
                    f'Zeile {line_no} (Id {record_id}, {name}): '
                    f'ungueltige Email {email!r} wird nicht importiert, Feld bleibt leer'
                )
                email = ''

            # Erstelle den Payload fuer die POST-Anfrage
            try:
                phone_e164 = phonenumbers.format_number(
                    phonenumbers.parse(row["Phone"], "CH"),
                    phonenumbers.PhoneNumberFormat.E164
                ).replace("+", "%2B")
            except phonenumbers.NumberParseException:
                log.warning(
                    f'Zeile {line_no} (Id {record_id}, {name}) uebersprungen: '
                    f'Telefonnummer {row["Phone"]!r} ist ungueltig. '
                    f'Bitte internationales Format wie 0041 6505551234 verwenden.'
                )
                continue

            payload = (
                f'data%5Bname%5D={urllib.parse.quote(name)}'
                f'&data%5Bphonebook_id%5D={phonebook_id}'
                f'&data%5Bphone%5D={phone_e164}'
                f'&data%5Bmobile%5D={row["Mobile"]}'
                f'&data%5Bemail%5D={urllib.parse.quote(email)}'
                f'&data%5Btype%5D={row["Type"]}'
                f'&data%5Borganization%5D={urllib.parse.quote(row["Organization"])}'
                f'&data%5Bnote%5D={row["Abteilung"]}'
            )

            # Sende die Daten an die REST-API
            try:
                response = requests.post(api_url, headers=headers, data=payload)
            except requests.exceptions.ConnectionError:
                log.warning('Connection Error: warte 10 sekunden')
                sleep(10)
                continue

            # Ueberpruefe die Antwort der API
            if response.status_code == 200:
                log.info(f'Zeile {line_no} (Id {record_id}, {name}): erfolgreich importiert.')
            else:
                log.error(
                    f'Zeile {line_no} (Id {record_id}, {name}): Fehler beim Senden. '
                    f'Statuscode: {response.status_code} Antwort: {response.text}'
                )
                exit_program()


def exit_program():
    log.info("Exiting the program...")
    sys.exit(0)


if __name__ == "__main__":
    del_contacts(config.phonebook_contacts_url)
    send_data_to_api(config.api_url, config.csv_file_path, config.phonebook_id)