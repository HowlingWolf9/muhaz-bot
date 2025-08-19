"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import discord
import sys
import os
import aiohttp
import update
import logging
import voicelink
import function as func

from discord.ext import commands
from discord import app_commands
from ipc import IPCClient
from motor.motor_asyncio import AsyncIOMotorClient
from logging.handlers import TimedRotatingFileHandler
from addons import Settings
import json
import asyncio
from aiohttp import web

# -------------------------------
# Load settings.json and replace ${ENV_VAR}
# -------------------------------
def load_settings(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def replace_env(obj):
        if isinstance(obj, dict):
            return {k: replace_env(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env(i) for i in obj]
        elif isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            env_var = obj[2:-1]
            value = os.getenv(env_var)
            if value is None:
                raise ValueError(f"Environment variable {env_var} not set!")
            return value
        else:
            return obj

    return replace_env(data)

func.settings = Settings(load_settings("settings.json"))

# -------------------------------
# Setup logging
# -------------------------------
LOG_SETTINGS = func.settings.logging
if (LOG_FILE := LOG_SETTINGS.get("file", {})).get("enable", True):
    log_path = os.path.abspath(LOG_FILE.get("path", "./logs"))
    os.makedirs(log_path, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=f'{log_path}/vocard.log',
        encoding="utf-8",
        backupCount=LOG_SETTINGS.get("max-history", 30),
        when="d"
    )
    file_handler.namer = lambda name: name.replace(".log", "") + ".log"
    file_handler.setFormatter(logging.Formatter(
        '{asctime} [{levelname:<8}] {name}: {message}',
        '%Y-%m-%d %H:%M:%S',
        style='{'
    ))
    logging.getLogger().addHandler(file_handler)

for log_name, log_level in LOG_SETTINGS.get("level", {}).items():
    _logger = logging.getLogger(log_name)
    _logger.setLevel(log_level)

# -------------------------------
# Translator
# -------------------------------
class Translator(discord.app_commands.Translator):
    async def load(self):
        func.logger.info("Loaded Translator")

    async def unload(self):
        func.logger.info("Unload Translator")

    async def translate(self, string: discord.app_commands.locale_str, locale: discord.Locale, context: discord.app_commands.TranslationContext):
        locale_key = str(locale)
        if locale_key in func.LOCAL_LANGS:
            translated_text = func.LOCAL_LANGS[locale_key].get(string.message)
            if translated_text is None:
                missing_translations = func.MISSING_TRANSLATOR.setdefault(locale_key, [])
                if string.message not in missing_translations:
                    missing_translations.append(string.message)
            return translated_text
        return None

# -------------------------------
# Bot class
# -------------------------------
class Vocard(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ipc: IPCClient

    async def on_message(self, message: discord.Message, /) -> None:
        if message.author.bot or not message.guild:
            return False

        if self.user.id in message.raw_mentions and not message.mention_everyone:
            prefix = await self.command_prefix(self, message)
            if not prefix:
                return await message.channel.send("I don't have a bot prefix set.")
            await message.channel.send(f"My prefix is `{prefix}`")

        settings = await func.get_settings(message.guild.id)
        if settings and (request_channel := settings.get("music_request_channel")):
            if message.channel.id == request_channel.get("text_channel_id"):
                ctx = await self.get_context(message)
                try:
                    cmd = self.get_command("play")
                    if message.content:
                        await cmd(ctx, query=message.content)
                    elif message.attachments:
                        for attachment in message.attachments:
                            await cmd(ctx, query=attachment.url)
                except Exception as e:
                    await func.send(ctx, str(e), ephemeral=True)
                finally:
                    return await message.delete()
        await self.process_commands(message)

    async def connect_db(self) -> None:
        if not ((db_name := func.settings.mongodb_name) and (db_url := func.settings.mongodb_url)):
            raise Exception("MONGODB_NAME and MONGODB_URL cannot be empty in settings.json")

        try:
            func.MONGO_DB = AsyncIOMotorClient(host=db_url)
            await func.MONGO_DB.server_info()
            func.logger.info(f"Successfully connected to [{db_name}] MongoDB!")
        except Exception as e:
            func.logger.error("Unable to connect to MongoDB!", exc_info=e)
            exit()

        func.SETTINGS_DB = func.MONGO_DB[db_name]["Settings"]
        func.USERS_DB = func.MONGO_DB[db_name]["Users"]

    async def setup_hook(self) -> None:
        func.langs_setup()
        await self.connect_db()
        await self.tree.set_translator(Translator())

        for module in os.listdir(func.ROOT_DIR + '/cogs'):
            if module.endswith('.py'):
                try:
                    await self.load_extension(f"cogs.{module[:-3]}")
                    func.logger.info(f"Loaded {module[:-3]}")
                except Exception as e:
                    func.logger.error(f"Error loading {module[:-3]} cog.", exc_info=e)

        self.ipc = IPCClient(self, **func.settings.ipc_client)
        if func.settings.ipc_client.get("enable", False):
            try:
                await self.ipc.connect()
            except Exception as e:
                func.logger.error(f"Cannot connect to IPC dashboard! - Reason: {e}")

        if not func.settings.version or func.settings.version != update.__version__:
            await self.tree.sync()
            func.update_json("settings.json", new_data={"version": update.__version__})
            for locale_key, values in func.MISSING_TRANSLATOR.items():
                func.logger.warning(f'Missing translation for "{", ".join(values)}" in "{locale_key}"')

    async def on_ready(self):
        func.logger.info("------------------")
        func.logger.info(f"Logged in as {self.user}")
        func.logger.info(f"Bot ID: {self.user.id}")
        func.logger.info("------------------")
        func.logger.info(f"Discord Version: {discord.__version__}")
        func.logger.info(f"Python Version: {sys.version}")
        func.logger.info("------------------")

        func.settings.client_id = self.user.id
        func.LOCAL_LANGS.clear()
        func.MISSING_TRANSLATOR.clear()

# -------------------------------
# Command prefix
# -------------------------------
async def get_prefix(bot: commands.Bot, message: discord.Message) -> str:
    settings = await func.get_settings(message.guild.id)
    prefix = settings.get("prefix", func.settings.bot_prefix)
    if prefix and not message.content.startswith(prefix) and (await bot.is_owner(message.author) or message.author.id in func.settings.bot_access_user):
        return ""
    return prefix

# -------------------------------
# Intents and bot instance
# -------------------------------
intents = discord.Intents.default()
intents.message_content = True if func.settings.bot_prefix else False
intents.members = func.settings.ipc_client.get("enable", False)
intents.voice_states = True

bot = Vocard(
    command_prefix=get_prefix,
    help_command=None,
    tree_cls=discord.app_commands.CommandTree,
    chunk_guilds_at_startup=False,
    activity=discord.Activity(type=discord.ActivityType.listening, name="Starting..."),
    case_insensitive=True,
    intents=intents
)

# -------------------------------
# Minimal web server for Render
# -------------------------------
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    func.logger.info(f"Web server running on port {port}")

# -------------------------------
# Run bot + web server concurrently
# -------------------------------
if __name__ == "__main__":
    update.check_version(with_msg=True)

    async def main():
        await start_web_server()
        await bot.start(func.settings.token)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        func.logger.info("Bot stopped manually.")