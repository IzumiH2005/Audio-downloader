import os
import logging
import asyncio
import uuid
from typing import Dict, Any, Optional

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler
)
from telegram.constants import ParseMode

from dotenv import load_dotenv
from yt_dlp import YoutubeDL
import sqlite3
import re
import hashlib
from datetime import datetime, timedelta

# Configuration des logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class YouTubeAudioDownloaderBot:
    # États de conversation pour les interactions complexes
    (SEARCH_QUERY, SELECT_RESULT) = range(2)

    def __init__(self):
        # Chargement des variables d'environnement
        load_dotenv()
        
        # Configuration de base
        self.TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
        self.ADMIN_ID = int(os.getenv('ADMIN_TELEGRAM_ID', 0))
        
        # Chemins et répertoires
        self.BASE_DIR = os.getcwd()
        self.DOWNLOAD_DIR = os.path.join(self.BASE_DIR, 'downloads')
        self.DB_PATH = os.path.join(self.BASE_DIR, 'bot_database.sqlite')
        
        # Créer les répertoires nécessaires
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        
        # Configuration avancée
        self.MAX_FILE_SIZE_MB = 50
        self.MAX_SEARCH_RESULTS = 5
        self.RATE_LIMIT_SECONDS = 30
        
        # Initialiser la base de données
        self._init_database()
        
        # Configuration de yt-dlp
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'nooverwrites': True,
            'no_color': True,
            'ignoreerrors': False,
            'geo_bypass': True,
            'quiet': True,
            'no_warnings': True,
            'outtmpl': os.path.join(self.DOWNLOAD_DIR, '%(id)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'max_filesize': self.MAX_FILE_SIZE_MB * 1024 * 1024
        }

    def _init_database(self):
        """Initialiser la base de données SQLite"""
        with sqlite3.connect(self.DB_PATH) as conn:
            cursor = conn.cursor()
            # Table des utilisateurs
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                join_date DATETIME,
                total_downloads INTEGER DEFAULT 0,
                last_download_date DATETIME
            )
            ''')
            
            # Table des téléchargements
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                video_id TEXT,
                title TEXT,
                download_date DATETIME,
                file_hash TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            ''')
            conn.commit()

    def _log_user_download(self, user: Dict[str, Any], video_info: Dict[str, Any], file_path: str):
        """Enregistrer les détails du téléchargement"""
        file_hash = self._calculate_file_hash(file_path)
        
        with sqlite3.connect(self.DB_PATH) as conn:
            cursor = conn.cursor()
            
            # Mise à jour ou insertion de l'utilisateur
            cursor.execute('''
            INSERT OR REPLACE INTO users 
            (user_id, username, first_name, last_name, join_date, total_downloads, last_download_date)
            VALUES (?, ?, ?, ?, ?, 
                COALESCE((SELECT total_downloads + 1 FROM users WHERE user_id = ?), 1),
                ?)
            ''', (
                user['id'], 
                user.get('username', ''), 
                user.get('first_name', ''), 
                user.get('last_name', ''),
                datetime.now(),
                user['id'],
                datetime.now()
            ))
            
            # Insertion du téléchargement
            cursor.execute('''
            INSERT INTO downloads 
            (user_id, video_id, title, download_date, file_hash)
            VALUES (?, ?, ?, ?, ?)
            ''', (
                user['id'], 
                video_info.get('id', 'unknown'),
                video_info.get('title', 'Unknown Title'),
                datetime.now(),
                file_hash
            ))
            
            conn.commit()

    def _calculate_file_hash(self, file_path: str) -> str:
        """Calculer le hash MD5 d'un fichier"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    async def start_command(self, update: Update, context):
        """Commande de démarrage du bot"""
        keyboard = [
            [
                InlineKeyboardButton("🔍 Rechercher", callback_data='search'),
                InlineKeyboardButton("❓ Aide", callback_data='help')
            ],
            [
                InlineKeyboardButton("📊 Mes Stats", callback_data='stats')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🎵 *Bienvenue sur YouTube Audio Downloader* 🎵\n\n"
            "Je peux télécharger l'audio de n'importe quelle vidéo YouTube !\n\n"
            "Choisissez une option :", 
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    async def callback_handler(self, update: Update, context):
        """Gérer les interactions avec les boutons"""
        query = update.callback_query
        await query.answer()

        if query.data == 'search':
            await query.edit_message_text(
                "🔍 Envoyez le titre ou l'URL de la vidéo à télécharger :"
            )
            return self.SEARCH_QUERY

        elif query.data == 'help':
            help_text = (
                "*🤖 Guide d'utilisation* \n\n"
                "• Envoyez un titre ou une URL YouTube\n"
                "• Le bot vous proposera les résultats\n"
                "• Sélectionnez la vidéo à télécharger\n\n"
                "_Limitations_ :\n"
                "• Fichiers < 50 MB\n"
                "• Téléchargement toutes les 30 secondes"
            )
            await query.edit_message_text(
                help_text, 
                parse_mode=ParseMode.MARKDOWN
            )

        elif query.data == 'stats':
            # Récupérer les statistiques de l'utilisateur
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT 
                        total_downloads, 
                        last_download_date,
                        (SELECT COUNT(*) FROM downloads WHERE user_id = ?) as unique_downloads
                    FROM users WHERE user_id = ?
                ''', (update.effective_user.id, update.effective_user.id))
                stats = cursor.fetchone()

            if stats:
                stats_text = (
                    f"*📊 Vos Statistiques* \n\n"
                    f"• Téléchargements totaux : {stats[0] or 0}\n"
                    f"• Fichiers uniques : {stats[2] or 0}\n"
                    f"• Dernier téléchargement : {stats[1] or 'Jamais'}"
                )
                await query.edit_message_text(
                    stats_text, 
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text("Aucune statistique disponible.")

    async def search_audio(self, update: Update, context):
        """Rechercher et proposer des résultats audio"""
        query = update.message.text
        
        # Message de recherche en cours
        search_message = await update.message.reply_text(
            "🔍 Recherche en cours...\n"
            "Veuillez patienter quelques secondes ⏳", 
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            with YoutubeDL(self.ydl_opts) as ydl:
                search_results = ydl.extract_info(f"ytsearch{self.MAX_SEARCH_RESULTS}:{query}", download=False)
                
            # Supprimer le message de recherche en cours
            await search_message.delete()
            
            if not search_results or 'entries' not in search_results:
                await update.message.reply_text("❌ Aucun résultat trouvé.")
                return ConversationHandler.END

            # Préparer les boutons de résultats
            keyboard = []
            for i, video in enumerate(search_results['entries'][:self.MAX_SEARCH_RESULTS], 1):
                button_text = f"{i}. {video['title'][:50]}..."
                keyboard.append([InlineKeyboardButton(
                    button_text, 
                    callback_data=f"select_video_{i-1}"
                )])
            
            keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            context.user_data['search_results'] = search_results['entries']
            
            await update.message.reply_text(
                "🔍 Résultats de recherche. Sélectionnez une vidéo :", 
                reply_markup=reply_markup
            )
            return self.SELECT_RESULT

        except Exception as e:
            # Supprimer le message de recherche en cours en cas d'erreur
            try:
                await search_message.delete()
            except:
                pass
            
            logger.error(f"Erreur de recherche : {e}")
            await update.message.reply_text("❌ Erreur lors de la recherche.")
            return ConversationHandler.END

    async def select_and_download(self, update: Update, context):
        """Télécharger la vidéo sélectionnée"""
        query = update.callback_query
        await query.answer()

        if query.data == 'cancel':
            await query.edit_message_text("❌ Recherche annulée.")
            return ConversationHandler.END

        try:
            # Extraire l'index de la vidéo
            match = re.match(r'select_video_(\d+)', query.data)
            if not match:
                await query.edit_message_text("❌ Sélection invalide.")
                return ConversationHandler.END

            index = int(match.group(1))
            search_results = context.user_data.get('search_results', [])
            
            if index >= len(search_results):
                await query.edit_message_text("❌ Vidéo non trouvée.")
                return ConversationHandler.END

            video = search_results[index]
            await query.edit_message_text(f"🔽 Téléchargement en cours : {video['title']}")

            # Logique de téléchargement
            with YoutubeDL(self.ydl_opts) as ydl:
                info = ydl.extract_info(video['webpage_url'], download=True)
                file_path = ydl.prepare_filename(info).replace('.webm', '.mp3')

            # Envoyer l'audio
            with open(file_path, 'rb') as audio:
                sent_audio = await update.effective_chat.send_audio(
                    audio, 
                    title=video['title'], 
                    performer=video.get('uploader', 'Unknown'),
                )

            # Log du téléchargement
            self._log_user_download(
                update.effective_user.to_dict(), 
                video, 
                file_path
            )

            # Nettoyage
            os.remove(file_path)
            await query.edit_message_text(f"✅ Téléchargé : {video['title']}")

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Erreur de téléchargement : {e}")
            await query.edit_message_text(f"❌ Erreur : {str(e)}")
            return ConversationHandler.END

    def setup_bot(self) -> Application:
        """Configuration du bot avec un gestionnaire de conversation"""
        application = Application.builder().token(self.TOKEN).build()

        # Gestionnaire de conversation pour recherche et téléchargement
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', self.start_command),
                CallbackQueryHandler(self.callback_handler)
            ],
            states={
                self.SEARCH_QUERY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.search_audio)
                ],
                self.SELECT_RESULT: [
                    CallbackQueryHandler(self.select_and_download)
                ]
            },
            fallbacks=[CommandHandler('cancel', self.start_command)]
        )

        application.add_handler(conv_handler)
        return application

    def run(self):
        """Lancement du bot"""
        try:
            logger.info("🚀 Bot YouTube Audio démarré...")
            application = self.setup_bot()
            application.run_polling(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Erreur lors du démarrage du bot : {e}")

def main():
    # Vérification FFmpeg
    try:
        import subprocess
        subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        print("FFmpeg est correctement installé ✅")
    except FileNotFoundError:
        print("❌ ERREUR : FFmpeg n'est pas installé !")

    bot = YouTubeAudioDownloaderBot()
    bot.run()

if __name__ == "__main__":
    main()