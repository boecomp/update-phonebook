#Konfiguration
 
#Individuelle Einstellungen
wildix_domain = 'domain'  # der Teil vor .wildixin.com (z.B. 'heag')
phonebook_id = 176        # change it to the real phonebook id
csv_file_path = '/pfad/zum/CSV/contacts.csv'
 
# Neuer Company API Key aus WMS -> PBX -> Integrations -> Company API Keys
# Benoetigter Scope fuer dieses Script: phonebooks:*  (oder pbx:*)
# Am besten NICHT hier im Klartext lassen, sondern als Umgebungsvariable setzen:
#   export WILDIX_API_KEY="wsk-v1-....."
# und unten den os.getenv Fallback nutzen.
import os
api_key = os.getenv('WILDIX_API_KEY', 'wsk-v1-DEIN-API-KEY-HIER')
 
#Standardpfade (neue, kleingeschriebene v1 Routen)
api_url = f'https://{wildix_domain}.wildixin.com/api/v1/contacts/'
phonebook_contacts_url = f'https://{wildix_domain}.wildixin.com/api/v1/phonebooks/{phonebook_id}/contacts/'
 