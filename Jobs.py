import os
import sqlite3
import feedparser
from dotenv import load_dotenv as lde
from email.mime.text import MIMEText
import smtplib
from pydantic import BaseModel, Field
from google import genai

# Charger les variables d'environnement
lde()

# ==========================================
# CONFIGURATION ET CONNEXION BDD (ANTI-DOUBLONS)
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API")
SENDER_EMAIL = os.getenv("GMAIL")
SENDER_PASSWORD = os.getenv("SECRET_KEY")
RECEIVER_EMAIL = os.getenv("GMAIL")
MODE_DEBUG = os.getenv("DEBUG", "False").lower() == "true"

client = genai.Client(api_key=GEMINI_API_KEY)

# Initialisation de la base SQLite pour stocker les offres déjà traitées
DB_NAME = "offres_vues.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS offres (
            id TEXT PRIMARY KEY,
            titre TEXT,
            source TEXT
        )
    ''')
    conn.commit()
    conn.close()

def offre_deja_traitee(offre_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM offres WHERE id = ?', (offre_id,))
    existe = cursor.fetchone() is not None
    conn.close()
    return existe

def marquer_comme_traitee(offre_id, titre, source):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO offres (id, titre, source) VALUES (?, ?, ?)', (offre_id, titre, source))
    conn.commit()
    conn.close()

# ==========================================
# SOURCES D'EMPLOI (URLs RSS / API corrigées)
# ==========================================
SOURCES_D_EMPLOI = [
    {"nom": "Remotive", "url": "https://remotive.com/api/remote-jobs"}, # Remotive API JSON
    {"nom": "We Work Remotely - Programming", "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
    {"nom": "We Work Remotely - DevOps & SysAdmin", "url": "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"},
    {"nom": "Remote OK", "url": "https://remoteok.com/remote-jobs.rss"},
    {"nom": "Himalayas Remote", "url": "https://himalayas.app/jobs/rss"},
    {"nom": "Python.org Jobs", "url": "https://www.python.org/jobs/feed/rss/"},
    {"nom": "Remote.co - Software Development", "url": "https://remote.co/feed/?post_type=job_listing"},
    {"nom": "Working Nomads - Tech", "url": "https://www.workingnomads.com/jobs/feed/all"}
]

# ==========================================
# SCHÉMA DE SORTIE STRUCTURÉE (Pydantic)
# ==========================================
class EvaluationOffre(BaseModel):
    valide: bool = Field(description="True si l'offre remplit tous les critères stricts, False sinon.")
    resume: str = Field(description="Résumé clair de 3 lignes en français contenant l'explication et les technos exigées si valide, sinon vide.")

PROMPT_INSTRUCTION = """
Tu es un agent de recrutement expert. Analyse la description de poste suivante selon ces critères stricts :
1. Domaine : Réseau, Cloud, Génie logiciel, Développement Web ou DevOps.
2. Contrat : À distance (Remote) basé en Europe OU ouvert à l'international (Worldwide).
3. Niveau : Intermédiaire (Mid-level, environ 2 à 5 ans d'expérience). Rejette les stages ou postes de Directeurs/Seniors avancés.
"""

def envoyer_email(titre, lien, source, resume):
    msg = MIMEText(f"Nouvelle opportunité trouvée !\n\nSource : {source}\nPoste : {titre}\nLien : {lien}\n\nAnalyse IA :\n{resume}", 'plain', 'utf-8')
    msg['Subject'] = f"🚨 Alerte Emploi [{source}] : {titre}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        print(f"✅ Mail envoyé pour : {titre}")
    except Exception as e:
        print(f"❌ Erreur envoi mail : {e}")

def executer_veille():
    init_db()
    print("🔄 Lancement de la collecte globale...")
    
    for source in SOURCES_D_EMPLOI:
        print(f"\n🌐 Extraction depuis : {source['nom']}")
        try:
            # Gestion basique si c'est l'API JSON de Remotive ou un flux RSS classique
            if "api/remote-jobs" in source['url']:
                import requests
                resp = requests.get(source['url'], timeout=10)
                data = resp.json().get('jobs', [])[:5]
                entries = []
                for item in data:
                    entries.append({
                        'id': str(item.get('id', item.get('url'))),
                        'title': item.get('title', 'Sans titre'),
                        'description': item.get('description', ''),
                        'link': item.get('url', '')
                    })
            else:
                flux = feedparser.parse(source['url'])
                entries = []
                for entry in flux.entries[:5]:
                    entries.append({
                        'id': entry.get('id', entry.get('link', '')),
                        'title': entry.get('title', 'Sans titre'),
                        'description': entry.get('description', entry.get('summary', '')),
                        'link': entry.get('link', '')
                    })

            for entry in entries:
                offre_id = entry['id']
                titre = entry['title']
                description = entry['description']
                lien = entry['link']

                if not offre_id:
                    continue

                # Vérifier si l'offre a déjà été traitée lors d'une précédente exécution
                if offre_deja_traitee(offre_id):
                    if MODE_DEBUG:
                        print(f"   ⏩ Déjà vue : {titre}")
                    continue

                if MODE_DEBUG:
                    print(f"   🔍 Analyse de : {titre}")

                # Appel à Gemini avec Structured Outputs
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=f"{PROMPT_INSTRUCTION}\n\nTitre : {titre}\nDescription :\n{description}",
                    config={
                        'response_mime_type': 'application/json',
                        'response_schema': EvaluationOffre,
                    },
                )
                
                import json
                resultat_json = json.loads(response.text)
                
                # Marquer l'offre comme traitée pour ne plus jamais l'analyser
                marquer_comme_traitee(offre_id, titre, source['nom'])

                if resultat_json.get("valide"):
                    print(f"   🎯 Offre validée : {titre}")
                    envoyer_email(titre, lien, source['nom'], resultat_json.get("resume"))
                else:
                    if MODE_DEBUG:
                        print(f"   ❌ Rejetée.")

        except Exception as e:
            print(f"⚠️ Erreur de lecture sur {source['nom']} : {e}")

if __name__ == "__main__":
    executer_veille() 
