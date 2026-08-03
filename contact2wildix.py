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
import requests
import phonenumbers
import config
import urllib.parse
import datetime
import sys
from time import sleep


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
        print(f'Fehler beim Abrufen der Kontakte. Statuscode: {response.status_code} '
              f'Antwort: {response.text} Timestamp: {current_time}')
        exit_program()

    data = response.json()
    # Je nach API-Response-Format liegt die Liste direkt in "result" oder in "result.contacts" -
    # hier beide Faelle abfangen.
    contacts = data.get('result', data)
    if isinstance(contacts, dict):
        contacts = contacts.get('contacts', [])

    if not contacts:
        print(f'Keine Kontakte im Telefonbuch gefunden. Timestamp: {current_time}')
        return

    print(f'{len(contacts)} Kontakte gefunden, werden geloescht...')

    # 2. Jeden Kontakt einzeln loeschen
    for contact in contacts:
        contact_id = contact.get('id')
        if contact_id is None:
            continue

        del_url = f'{phonebook_contacts_url}{contact_id}/'
        del_response = requests.delete(del_url, headers=headers)
        current_time = datetime.datetime.now()

        if del_response.status_code == 200:
            print(f'Kontakt {contact_id} erfolgreich geloescht. Timestamp: {current_time}')
        else:
            print(f'Fehler beim Loeschen von Kontakt {contact_id}. '
                  f'Statuscode: {del_response.status_code} Antwort: {del_response.text} '
                  f'Timestamp: {current_time}')


# Funktion zum Pruefen und Senden der Daten an die REST-API
def send_data_to_api(api_url, csv_file, phonebook_id):
    headers = get_headers()

    # Lese die CSV-Datei
    with open(csv_file, mode='r', encoding='utf-8') as file:
        csv_reader = csv.DictReader(file)
        for row in csv_reader:

            # Erstelle den Payload fuer die POST-Anfrage
            try:
                phone_e164 = phonenumbers.format_number(
                    phonenumbers.parse(row["Phone"], "CH"),
                    phonenumbers.PhoneNumberFormat.E164
                ).replace("+", "%2B")
            except phonenumbers.NumberParseException:
                print(f'Phone number: {row["Phone"]} is not valid. '
                      f'Please use the international format like 0041 6505551234')
                continue

            payload = (
                f'data%5Bname%5D={urllib.parse.quote(row["Name"])}'
                f'&data%5Bphonebook_id%5D={phonebook_id}'
                f'&data%5Bphone%5D={phone_e164}'
                f'&data%5Bmobile%5D={row["Mobile"]}'
                f'&data%5Bemail%5D={row["Email"]}'
                f'&data%5Btype%5D={row["Type"]}'
                f'&data%5Borganization%5D={urllib.parse.quote(row["Organization"])}'
                f'&data%5Bnote%5D={row["Abteilung"]}'
            )

            # Sende die Daten an die REST-API
            try:
                response = requests.post(api_url, headers=headers, data=payload)
            except requests.exceptions.ConnectionError:
                print('Connection Error: warte 10 sekunden')
                sleep(10)
                continue

            # Ueberpruefe die Antwort der API
            current_time = datetime.datetime.now()
            if response.status_code == 200:
                print(f'Daten fuer {row["Name"]} erfolgreich an die API gesendet. Timestamp: {current_time}')
            else:
                print(f'Fehler beim Senden der Daten fuer {row["Name"]}. '
                      f'Statuscode: {response.status_code} Antwort: {response.text} Timestamp: {current_time}')
                exit_program()


def exit_program():
    print("Exiting the program...")
    sys.exit(0)


if __name__ == "__main__":
    del_contacts(config.phonebook_contacts_url)
    send_data_to_api(config.api_url, config.csv_file_path, config.phonebook_id)