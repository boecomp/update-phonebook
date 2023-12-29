#Konfiguration

#Individuelle Einstellungen
wildix_domain = 'domain' #the part before .wildixin.com
phonebook_id = 176 #change it to the real phonebook id
api_id = 'api id from pbx' #create a application in the pbx section integrations and ass here the id and secret key
api_secret = 'secret key from PBX'
csv_file_path = '/pfad/zum/CSV/contacts.csv'

#Standardpfade
api_url = f'https://{wildix_domain}.wildixin.com/api/v1/Contacts/'
api_del_url = f'https://{wildix_domain}.wildixin.com/api/v1/Phonebooks/{phonebook_id}/Contacts/'
