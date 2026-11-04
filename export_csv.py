# C:\projet_anpr\test\export_csv.py  — remplace tout le fichier par ça :
import sqlite3, csv, sys
conn = sqlite3.connect('C:/projet_anpr/test/infractions.db')
rows = conn.execute('SELECT horodatage,plaque,type,statut,nom,prenom,carte_grise,email,telephone FROM infractions ORDER BY id DESC').fetchall()
conn.close()
sys.stdout.reconfigure(encoding='utf-8')
w = csv.writer(sys.stdout)
w.writerow(['horodatage','plaque','type','statut','nom','prenom','carte_grise','email','telephone'])
w.writerows(rows)