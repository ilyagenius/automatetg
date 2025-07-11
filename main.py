#!/usr/bin/env python3
import os
import sys
import time
import asyncio
import logging
import requests
import gspread
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from telethon import TelegramClient, events, errors, Button
from telethon.tl.types import InputPeerChannel, PeerUser
import openai
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

# ====== HARDENED SETTINGS ====== #
# Telegram
API_ID = ""
API_HASH = ""
PHONE = ""
SESSION_NAME = ""
CHANNEL_ID = ""
ADMIN_ID =   # Ваш ID администратора
PROXY = None  # ('socks5', 'ip', port, username='', password='')
BOT_TOKEN = ""  # Токен вашего бота для аппрува

# OpenAI
OPENAI_API_KEY = ""
GPT_MODEL = "gpt-4o"

# Google Sheets
GOOGLE_CREDS = "credentials.json"
SHEET_ID = "11qcSUsmzvUxg_8BKr4uPJSow_eXhJLOgJ1aL_QoRceo"

# Image Settings
IMAGE_BASE_DIR = "images"  # Base directory for images

# System
MAX_RETRIES = 10
FLOOD_WAIT_MAX = 300  # 5 minutes
RECONNECT_BASE_DELAY = 10
APPROVAL_TIMEOUT = 600  # 10 minutes for approval

# ====== STATE MANAGEMENT ====== #
class ApprovalState:
    def __init__(self):
        self.states = {}
        self.lock = asyncio.Lock()
    
    async def create_state(self, user_id, topic, text, image_path):
        async with self.lock:
            self.states[user_id] = {
                'topic': topic,
                'text': text,
                'image_path': image_path,
                'text_approved': False,
                'image_approved': False,
                'created_at': time.time(),
                'text_message_id': None,
                'image_message_id': None,
                'text_feedback': None,
                'image_feedback': None,
                'awaiting_feedback': None,  # 'text' или 'image'
                'edit_history': []  # История правок текста
            }
    
    async def get_state(self, user_id):
        async with self.lock:
            return self.states.get(user_id)
    
    async def update_state(self, user_id, update_dict):
        async with self.lock:
            if user_id in self.states:
                self.states[user_id].update(update_dict)
    
    async def delete_state(self, user_id):
        async with self.lock:
            if user_id in self.states:
                del self.states[user_id]
    
    async def add_edit(self, user_id, text, feedback):
        """Добавляет версию текста в историю правок"""
        async with self.lock:
            if user_id in self.states:
                self.states[user_id]['edit_history'].append({
                    'text': text,
                    'feedback': feedback,
                    'timestamp': time.time()
                })
    
    async def get_last_text_version(self, user_id):
        """Возвращает последнюю версию текста"""
        async with self.lock:
            if user_id in self.states:
                if self.states[user_id]['edit_history']:
                    return self.states[user_id]['edit_history'][-1]['text']
                return self.states[user_id]['text']
            return None
    
    async def cleanup_expired(self):
        async with self.lock:
            current_time = time.time()
            expired_users = [
                uid for uid, state in self.states.items()
                if current_time - state['created_at'] > APPROVAL_TIMEOUT
            ]
            for uid in expired_users:
                del self.states[uid]
            return len(expired_users)

# Global state manager
approval_manager = ApprovalState()

# ====== BULLETPROOF LOGGER ====== #
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)8s | %(name)12s | %(message)s',
    handlers=[
        logging.FileHandler("bot_audit.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TerminatorBot")

# ====== ARMORED INITIALIZATION ====== #
def init_openai():
    return openai.OpenAI(api_key=OPENAI_API_KEY)

def init_openai_async():
    return openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

async def create_telegram_client():
    return TelegramClient(
        session=SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        proxy=PROXY,
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=True,
        device_model="Server",
        system_version="Linux/6.7.0",
        app_version="3.0"
    )

async def create_bot_client():
    client = TelegramClient(
        session='approval_bot',
        api_id=API_ID,
        api_hash=API_HASH
    )
    await client.start(bot_token=BOT_TOKEN)
    return client

# ====== FAILPROOF CORE FUNCTIONS ====== #
@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(Exception)
)
def get_today_topic():
    """Nuclear-proof Google Sheets fetcher"""
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            GOOGLE_CREDS,
            ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        )
        sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
        today = datetime.now().strftime("%m/%d/%Y")
        
        for row in sheet.get_all_records():
            if str(row.get('Date', '')).strip() == today:
                topic = row.get('Topic', 'No topic found')
                return topic 
        return 'No topic defined'
    except Exception as e:
        logger.error(f"Google Sheets Armageddon: {e}")
        raise

@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=30)
)
async def generate_text_async(topic, user_feedback=None):
    """EMP-resistant text generator with user feedback"""
    try:
        client = init_openai_async()
        
        # Base prompt
        base_prompt = f"""Копирайтер ТГ канала

🧠 Роль:

Ты — опытный контент-менеджер и креативный копирайтер, специализирующийся на создании увлекательного и полезного контента для Telegram-каналов о здоровье, спорте и правильном питании.
Ты досконально знаешь лучшие практики форматирования текста в Telegram и умеешь создавать статьи, которые цепляют взгляд и вызывают вовлечённость.
Сегодняшняя тема - {topic}

⸻

🎯 Задача:

Создавать информативные, мотивирующие посты для Telegram-канала «Активный код», строго по структуре и стилю.

⸻

🧭 Инструкция:
	1.	Получи команду от пользователя:
«Давай напишем статью под номером [X]»
	2.	Найди тему в контент-плане (Google-таблица).
	3.	Сгенерируй текст статьи на основе темы, строго соблюдая структуру и формат.

⸻

📝 Формат статьи (только текст):
	•	Без вводных слов («вот статья», «готово», «держи» и т.п.)
	•	Начинай сразу с жирного заголовка.
	•	Используй:
	•	Жирный шрифт — для заголовков и подзаголовков
	•	Курсив — для мотивации и эмоционального акцента
	•	📌 Эмодзи — умеренно, только уместные
	•	#Хэштеги — в конце, по теме

⸻

📐 Структура статьи:

1. Введение
Цепляющее, краткое. Вопрос, факт, боль, сравнение, интрига.

2. Зачем?
Польза, функции. Конкретные примеры, метафоры.
Используй маркированные списки.

3. Когда и как?
Инструкции, частота, сочетания.
Используй нумерованные списки.

4. Сколько?
Формулы, расчёты по весу и уровню активности. Примеры блюд и продуктов.

5. Ошибки/Мифы
Развенчание заблуждений. Яркие контрасты, юмор, эмодзи.

6. Вывод
Краткое резюме, мотивация, призыв к действию, вопрос к аудитории.

⸻

👄 Стиль:
	•	Разговорный, лёгкий, «живой», на «ты» или «вам»
	•	Краткие фразы, ясные образы
	•	Юмор (6–7 из 10)
	•	До 950 символов включая пробелы(Строго!)
	•	Сильная визуальная и логическая структура

⸻
"""
        
        # Add user feedback if provided
        full_prompt = base_prompt.format(topic=topic)
        if user_feedback:
            full_prompt = f"USER FEEDBACK (HIGH PRIORITY):\n{user_feedback}\n\n{full_prompt}"

        # Text generation
        text_response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": "Professional fitness copywriter, пиши строго до 950 символов включая пробелы!!!"},
                {"role": "user", "content": full_prompt}
            ],
            max_tokens=550,
            temperature=0.5
        )
        return text_response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI Text Meltdown: {e}")
        raise

@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=30)
)
async def edit_text_async(text, feedback, topic):
    """Professional text editor with feedback"""
    try:
        client = init_openai_async()
        
        # Professional editor prompt
        edit_prompt = f"""
        Ты профессиональный редактор фитнес-контента с 10-летним опытом. Тебе нужно отредактировать текст поста, 
        строго следуя инструкциям редактора, сохраняя структуру и стиль.

        Тема: {topic}
        Инструкции редактора: {feedback}

        Текст для редактирования:
        {text}

        Задачи:
        1. Внеси все запрошенные изменения
        2. Сохрани оригинальную структуру и стиль
        3. Убедись, что текст не превышает 950 символов
        4. Сохрани разметку Markdown (жирный, курсив, списки)
        5. Не добавляй новые разделы без запроса

        Редактируй текст идеально, точно следуя инструкциям.
        """
        
        # Text editing
        edit_response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": "Professional editor for fitness content"},
                {"role": "user", "content": edit_prompt}
            ],
            max_tokens=600,
            temperature=0.3
        )
        return edit_response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI Edit Meltdown: {e}")
        raise

def get_today_image():
    """Get first image from today's date folder"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        folder_path = os.path.join(IMAGE_BASE_DIR, today)
        
        if not os.path.exists(folder_path):
            logger.error(f"Image folder not found: {folder_path}")
            return None
            
        # Find first image file
        for file in os.listdir(folder_path):
            if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                return os.path.join(folder_path, file)
                
        logger.error(f"No images found in: {folder_path}")
        return None
    except Exception as e:
        logger.error(f"Image retrieval failed: {e}")
        return None

# ====== TELEGRAM WARRIOR FUNCTIONS ====== #
async def send_to_channel(client, text, image_path=None):
    """Tank-grade message sender to channel"""
    try:
        if image_path:
            # Send with armored caption
            await client.send_file(
                entity=CHANNEL_ID,
                file=image_path,
                caption=text,
                parse_mode="Markdown"
            )
        else:
            # Split long messages like a samurai
            for i in range(0, len(text), 4096):
                await client.send_message(
                    entity=CHANNEL_ID,
                    message=text[i:i+4096],
                    parse_mode='Markdown'
                )
                await asyncio.sleep(1)  # Respect rate limits
        return True
    except Exception as e:
        logger.error(f"Channel Message Delivery Failed: {e}")
        return False

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
async def send_image_to_admin(bot_client, user_id, image_path, caption, buttons):
    """Secure image sender to admin with retries"""
    try:
        with open(image_path, 'rb') as f:
            msg = await bot_client.send_file(
                entity=user_id,
                file=f,
                caption=caption,
                buttons=buttons,
                parse_mode='md',
                timeout=60
            )
        return msg
    except Exception as e:
        logger.error(f"Admin Image Send Failed: {e}")
        return None

# ====== APPROVAL FLOW FUNCTIONS ====== #
async def start_approval_flow(bot_client, user_id, topic):
    """Initiate the post approval workflow"""
    try:
        # Notify admin using bot client
        await bot_client.send_message(user_id, "⚙️ Starting content generation...")
        
        # Generate text
        text = await generate_text_async(topic)
        
        # Get today's image
        image_path = get_today_image()
        if not image_path:
            await bot_client.send_message(user_id, "⚠️ No image found for today!")
            return
            
        # Create approval state
        await approval_manager.create_state(user_id, topic, text, image_path)
        
        # Add initial version to history
        await approval_manager.add_edit(user_id, text, "Initial generation")
        
        # Send text for approval
        buttons = [
            [Button.inline("✅ Approve Text", b"approve_text")],
            [Button.inline("🔄 Edit Text", b"regenerate_text")],
            [Button.inline("❌ Cancel", b"cancel_approval")]
        ]
        
        msg = await bot_client.send_message(
            entity=user_id,
            message=f"**Generated Text:**\n\n{text}",
            buttons=buttons,
            parse_mode='md'
        )
        
        # Save message ID
        await approval_manager.update_state(
            user_id, 
            {'text_message_id': msg.id}
        )
        
    except Exception as e:
        logger.error(f"Approval Flow Init Failure: {e}")
        await bot_client.send_message(user_id, f"⚠️ Approval flow failed: {str(e)[:200]}")

async def handle_text_approval(bot_client, event, state):
    """Process text approval actions"""
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    if data == "approve_text":
        # Update state
        await approval_manager.update_state(
            user_id, 
            {'text_approved': True}
        )
        
        await event.answer("Text approved! Processing image...")
        
        # Prepare image for approval
        caption = f"Сегодняшнее изображение для поста"
        buttons = [
            [Button.inline("✅ Approve Image", b"approve_image")],
            [Button.inline("❌ Cancel", b"cancel_approval")]
        ]
        
        # Send image with buttons
        msg = await send_image_to_admin(
            bot_client,
            user_id,
            state['image_path'],
            caption,
            buttons
        )
        
        if not msg:
            await event.reply("⚠️ Failed to send image. Please try again.")
            return
            
        # Save message ID
        await approval_manager.update_state(
            user_id, 
            {'image_message_id': msg.id}
        )
        
    elif data == "regenerate_text":
        # Set state to await feedback
        await approval_manager.update_state(
            user_id,
            {'awaiting_feedback': 'text'}
        )
        
        # Get last text version
        last_text = await approval_manager.get_last_text_version(user_id)
        
        # Edit message to ask for feedback
        await bot_client.edit_message(
            entity=user_id,
            message=state['text_message_id'],
            text=f"**Текущий текст:**\n\n{last_text}\n\n✏️ **Опишите, что нужно изменить:**",
            buttons=None,
            parse_mode='md'
        )
        await event.answer("Awaiting your feedback...")
        
    elif data == "cancel_approval":
        await approval_manager.delete_state(user_id)
        await event.answer("Approval cancelled!")
        await bot_client.send_message(user_id, "❌ Post approval cancelled.")

async def handle_image_approval(bot_client, user_client, event, state):
    """Process image approval actions"""
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    if data == "approve_image":
        # Update state
        await approval_manager.update_state(
            user_id, 
            {'image_approved': True}
        )
        
        await event.answer("Image approved! Publishing to channel...")
        
        # Get last approved text version
        last_text = await approval_manager.get_last_text_version(user_id)
        
        # Send to channel using main client
        success = await send_to_channel(
            user_client, 
            last_text, 
            state['image_path']
        )
        
        if success:
            await bot_client.send_message(user_id, "✅ Post published successfully!")
        else:
            await bot_client.send_message(user_id, "⚠️ Failed to publish post. Please try again.")
        
        # Cleanup
        await approval_manager.delete_state(user_id)
        
    elif data == "cancel_approval":
        await approval_manager.delete_state(user_id)
        await event.answer("Approval cancelled!")
        await bot_client.send_message(user_id, "❌ Post approval cancelled.")

# ====== FEEDBACK HANDLER ====== #
async def handle_feedback(bot_client, user_client, event, state):
    """Handle user feedback for text editing"""
    user_id = event.sender_id
    feedback_text = event.raw_text
    
    # Only text feedback is supported
    if state.get('awaiting_feedback') != 'text':
        await event.reply("⚠️ Only text feedback is supported")
        return
    
    # Get last text version
    last_text = await approval_manager.get_last_text_version(user_id)
    
    # Reset feedback state
    await approval_manager.update_state(
        user_id,
        {
            'awaiting_feedback': None,
            'text_feedback': feedback_text
        }
    )
    
    # Notify user
    await event.reply("🔄 Editing text based on your feedback...")
    
    try:
        # Edit text with professional editor
        edited_text = await edit_text_async(
            text=last_text,
            feedback=feedback_text,
            topic=state['topic']
        )
        
        # Add to edit history
        await approval_manager.add_edit(user_id, edited_text, feedback_text)
        
        # Update state with new text
        await approval_manager.update_state(
            user_id,
            {'text': edited_text}
        )
        
        # Show edited text
        buttons = [
            [Button.inline("✅ Approve Text", b"approve_text")],
            [Button.inline("🔄 Edit Text", b"regenerate_text")],
            [Button.inline("❌ Cancel", b"cancel_approval")]
        ]
        
        msg = await bot_client.send_message(
            entity=user_id,
            message=f"**Edited Text:**\n\n{edited_text}",
            buttons=buttons,
            parse_mode='md'
        )
        
        # Save message ID
        await approval_manager.update_state(
            user_id,
            {'text_message_id': msg.id}
        )
            
    except Exception as e:
        logger.error(f"Text editing failed: {e}")
        await event.reply(f"⚠️ Editing error: {str(e)[:200]}")

# ====== STATE CLEANUP TASK ====== #
async def state_cleanup_task():
    """Periodically clean up expired approval states"""
    while True:
        try:
            cleaned = await approval_manager.cleanup_expired()
            if cleaned > 0:
                logger.info(f"🧹 Cleaned up {cleaned} expired approval states")
            await asyncio.sleep(300)  # Run every 5 minutes
        except Exception as e:
            logger.error(f"State cleanup error: {e}")
            await asyncio.sleep(60)

# ====== SELF-HEALING CORE ====== #
async def run_bot(user_client):
    """Run the approval bot"""
    bot_client = await create_bot_client()
    me = await bot_client.get_me()
    logger.info(f"🤖 Approval Bot started as @{me.username}")
    
    # Start state cleanup task
    asyncio.create_task(state_cleanup_task())
    
    # Command handler
    @bot_client.on(events.NewMessage(pattern='/generate'))
    async def generate_handler(event):
        if event.sender_id != ADMIN_ID:
            await event.reply("🚫 You are not authorized to use this command.")
            return
            
        try:
            topic = await asyncio.to_thread(get_today_topic)
            await start_approval_flow(bot_client, event.sender_id, topic)
        except Exception as e:
            logger.error(f"Generate Command Failure: {e}")
            await event.reply(f"⚠️ Command failed: {str(e)[:200]}")

    # Start command handler
    @bot_client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        if event.sender_id == ADMIN_ID:
            await event.reply("🦾 Terminator Bot v4.0 Activated!\n"
                             "Use /generate to create new post")
        else:
            await event.reply("⛔ Access Denied")

    # Callback handler
    @bot_client.on(events.CallbackQuery())
    async def callback_handler(event):
        if event.sender_id != ADMIN_ID:
            await event.answer("🚫 You are not authorized!")
            return
            
        state = await approval_manager.get_state(event.sender_id)
        if not state:
            await event.answer("❌ No active approval session!")
            return
            
        try:
            # Handle based on current state
            if not state['text_approved']:
                await handle_text_approval(bot_client, event, state)
            elif not state['image_approved']:
                await handle_image_approval(bot_client, user_client, event, state)
        except Exception as e:
            logger.error(f"Callback Handler Failure: {e}")
            await event.answer("⚠️ Operation failed!")
            await bot_client.send_message(event.sender_id, f"❌ Approval flow error: {str(e)[:200]}")
    
    # Feedback handler
    @bot_client.on(events.NewMessage())
    async def feedback_message_handler(event):
        if event.sender_id != ADMIN_ID:
            return
            
        state = await approval_manager.get_state(event.sender_id)
        if not state or not state.get('awaiting_feedback'):
            return
            
        # Process feedback
        await handle_feedback(bot_client, user_client, event, state)
    
    await bot_client.run_until_disconnected()

async def immortal_bot():
    """Phoenix-like bot that never dies"""
    reconnect_delay = RECONNECT_BASE_DELAY
    consecutive_failures = 0
    
    # Initialize OpenAI async client
    global openai_async
    openai_async = init_openai_async()
    
    while True:
        try:
            async with await create_telegram_client() as user_client:
                # Start the approval bot in background
                bot_task = asyncio.create_task(run_bot(user_client))
                
                logger.info("🛡️ Main client connected")
                await user_client.run_until_disconnected()
                
                consecutive_failures = 0
                reconnect_delay = RECONNECT_BASE_DELAY

        except errors.FloodWaitError as e:
            wait_time = min(e.seconds + 5, FLOOD_WAIT_MAX)
            logger.warning(f"⏳ Flood control: sleeping {wait_time}s")
            await asyncio.sleep(wait_time)
            
        except (errors.ConnectionError, errors.OperationCancelledError) as e:
            consecutive_failures += 1
            backoff = min(reconnect_delay * (2 ** consecutive_failures), 300)
            logger.error(f"🌐 Connection failure #{consecutive_failures}: {e}")
            logger.info(f"♻️ Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            
        except Exception as e:
            logger.critical(f"💀 Apocalyptic failure: {e}")
            logger.info("🔄 Attempting resurrection...")
            await asyncio.sleep(RECONNECT_BASE_DELAY)
        finally:
            # Cancel bot task when main client disconnects
            if 'bot_task' in locals():
                bot_task.cancel()

# ====== LAUNCH SEQUENCE ====== #
if __name__ == "__main__":
    logger.info("🚀 Starting Terminator Bot v4.0")
    
    # Nuclear launch codes verification
    required_files = [GOOGLE_CREDS]
    for file in required_files:
        if not os.path.exists(file):
            logger.critical(f"Missing critical file: {file}")
            sys.exit(1)
    
    # Verify images directory exists
    if not os.path.exists(IMAGE_BASE_DIR):
        os.makedirs(IMAGE_BASE_DIR)
        logger.info(f"Created image directory: {IMAGE_BASE_DIR}")
    
    # Activate Skynet
    try:
        asyncio.run(immortal_bot())
    except KeyboardInterrupt:
        logger.info("🛑 Manual shutdown detected")
    except Exception as e:
        logger.critical(f"DOOMSDAY: {e}")
    finally:
        logger.info("☠️ Bot process terminated")
