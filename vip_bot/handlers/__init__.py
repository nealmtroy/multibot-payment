from vip_bot.handlers.user import register_user_handlers
from vip_bot.handlers.admin import register_admin_handlers

def register_handlers(client, config, store, qris_semaphore, user_locks, withdrawal_states, bot_manager=None, bot_code="default"):
    register_user_handlers(client, config, store, qris_semaphore, user_locks, withdrawal_states, bot_code=bot_code)
    register_admin_handlers(client, config, store, qris_semaphore, user_locks, bot_manager=bot_manager)
