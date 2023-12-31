#CSV import in Wildix PBX Telefonbücher - die Konfig muss im config.py angepasst werden

import csv
import requests
import phonenumbers
import config
import urllib.parse
from time import sleep

# Funktion zum löschen der Daten im Telefonbuch
def del_contacts(api_del_url):
    payload = {}
    headers = {
      'Cookie': 'PHPSESSID=liaia58fh1l8pb344mmkr3qar4; httpsOnly=1'
    }

    response = requests.request("DELETE", api_del_url, headers=headers, data=payload)

    #print(response.text)

# Funktion zum Prüfen und Senden der Daten an die REST-API
def send_data_to_api(api_url, api_id, api_secret, csv_file, phonebook_id):
    headers = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    api_id: api_secret,
    'Cookie': 'PHPSESSID=0lo981u5jd99ko17oo1m6k1tav; httpsOnly=1'
    }

    # Lese die CSV-Datei
    with open(csv_file, mode='r', encoding='utf-8') as file:
        csv_reader = csv.DictReader(file)
        line_count = 0
        for row in csv_reader:

            # Erstellen Sie einen Payload für die POST-Anfrage
            try:
                payload = f'data%5Bname%5D={urllib.parse.quote(row["Name"])}&data%5Bphonebook_id%5D={phonebook_id}&data%5Bphone%5D={phonenumbers.format_number(phonenumbers.parse(row["Phone"], "CH"), phonenumbers.PhoneNumberFormat.E164).replace("+","%2B")}&data%5Bmobile%5D={row["Mobile"]}&data%5Bemail%5D={row["Email"]}&data%5Btype%5D={row["Type"]}&data%5Borganization%5D={urllib.parse.quote(row["Organization"])}&data%5Bnote%5D={row["Abteilung"]}'
                #print(payload)
            except phonenumbers.NumberParseException:
                print(f'Phone number: {row["Phone"]} is not valid. Please use the international format like 0041 6505551234')

            # Sende die Daten an die REST-API
            try:
                response = requests.post(api_url, headers=headers, data=payload)
            except requests.exceptions.ConnectionError:
                print('Connection Error: warte 10 sekunden')
                sleep(10)

            # Überprüfe die Antwort der API
            if response.status_code == 200:
                print(f'Daten für {row["Name"]} erfolgreich an die API gesendet.')
            else:
                print(f'Fehler beim Senden der Daten für {row["Name"]}. Statuscode: {response.status_code}')


del_contacts(config.api_del_url)
send_data_to_api(config.api_url, config.api_id, config.api_secret, config.csv_file_path, config.phonebook_id)
