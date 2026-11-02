"""Pure logic of the media-stack Telegram bot.

releases: rules for picking, offering and removing releases and torrents.
text:     HTML-safe message helpers (escaping, durations, cards, episode codes).
langs:    ISO 639-1 language table and the per-language movie libraries.

Nothing in this package reads the environment, files or the network, so the test-suite can
import it without python-telegram-bot or a running stack.
"""
