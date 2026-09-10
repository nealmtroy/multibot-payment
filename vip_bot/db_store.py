import time
import random
import logging
import httpx
from supabase import create_client
from vip_bot.config import env_int, ACTIVE_PAYMENT_STATUSES, RETRYABLE_PAYMENT_STATUSES

LOGGER = logging.getLogger('telegram_vip_bot.db_store')


def os_getenv_wrapper(key, default=''):
    import os
    return os.getenv(key, default).strip()


class PaymentStore:
    def __init__(self, config):
        self.table = config.supabase_table
        self.package_table = config.supabase_package_table
        self.user_table = config.user_table
        self.referral_table = config.referral_table
        self.withdrawal_table = config.withdrawal_table
        self.broadcast_table = os_getenv_wrapper('SUPABASE_BROADCAST_TABLE', 'vip_broadcast_messages')
        self.settings_table = os_getenv_wrapper('SUPABASE_SETTINGS_TABLE', 'vip_bot_settings')
        self.bot_table = os_getenv_wrapper('SUPABASE_BOT_TABLE', 'vip_bots')
        self.client = create_client(config.supabase_url, config.supabase_service_role_key)
        self.query_retries = max(1, env_int('SUPABASE_QUERY_RETRIES', 3))
        self.retry_base_delay = max(0.1, float(os_getenv_wrapper('SUPABASE_RETRY_BASE_DELAY', '0.35')))

    def _execute(self, query, action):
        for attempt in range(1, self.query_retries + 1):
            try:
                return query.execute()
            except httpx.TransportError as exc:
                if attempt >= self.query_retries:
                    raise
                delay = min(4.0, self.retry_base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.15)
                LOGGER.warning(
                    'Transient Supabase transport error during %s, retrying in %.2fs (%s/%s): %s',
                    action,
                    delay,
                    attempt,
                    self.query_retries,
                    exc,
                )
                time.sleep(delay)

    # -------------------------------------------------------------------------
    # Bot Management Methods
    # -------------------------------------------------------------------------

    def list_active_bots(self):
        query = self.client.table(self.bot_table).select('*').eq('status', 'active').order('id', desc=False)
        response = self._execute(query, 'list active bots')
        return response.data or []

    def list_all_bots(self):
        query = self.client.table(self.bot_table).select('*').order('id', desc=False)
        response = self._execute(query, 'list all bots')
        return response.data or []

    def get_bot(self, bot_code):
        query = self.client.table(self.bot_table).select('*').eq('bot_code', bot_code).limit(1)
        response = self._execute(query, 'get bot')
        return response.data[0] if response.data else None

    def upsert_bot(self, bot_code, bot_token, bot_username='', bot_name='', status='active'):
        from vip_bot.helpers import utc_now_iso
        now = utc_now_iso()
        data = {
            'bot_code': bot_code,
            'bot_token': bot_token,
            'bot_username': bot_username,
            'bot_name': bot_name or bot_code,
            'status': status,
            'updated_at': now,
        }
        query = self.client.table(self.bot_table).upsert(data, on_conflict='bot_code')
        response = self._execute(query, 'upsert bot')
        return response.data[0] if response.data else data

    def set_bot_status(self, bot_code, status):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.bot_table).update(
            {'status': status, 'updated_at': utc_now_iso()}
        ).eq('bot_code', bot_code)
        response = self._execute(query, f'set bot status {status}')
        return bool(response.data)

    def delete_bot(self, bot_code):
        query = self.client.table(self.bot_table).delete().eq('bot_code', bot_code)
        response = self._execute(query, 'delete bot')
        return bool(response.data)

    # -------------------------------------------------------------------------
    # Payment Methods
    # -------------------------------------------------------------------------

    def create_payment(
        self,
        user,
        public_invoice_id,
        order_id,
        payment_url,
        inv_id,
        amount,
        buyer_name,
        buyer_email,
        qris_data,
        qris_chat_id,
        qris_message_id,
        package=None,
        referral=None,
        bot_code='default',
    ):
        from vip_bot.helpers import utc_now_iso, parse_iso_datetime, next_poll_at, display_name
        import datetime as dt
        now = utc_now_iso()
        payload = qris_data.get('data', {})
        package = package or {}
        expires_at = parse_iso_datetime(payload.get('countdown') or '')
        next_check_at = next_poll_at(dt.datetime.now(dt.UTC), expires_at, attempts=0, error='')
        data = {
            'bot_code': bot_code,
            'user_id': user.id,
            'username': user.username or '',
            'full_name': display_name(user),
            'package_code': package.get('code') or '',
            'package_name': package.get('name') or '',
            'package_amount': int(package.get('amount') or amount),
            'vip_chat_id': package.get('vip_chat_id'),
            'invite_expire_hours': int(package.get('invite_expire_hours') or 0),
            'public_invoice_id': public_invoice_id,
            'order_id': order_id,
            'payment_url': payment_url,
            'inv_id': inv_id,
            'amount': amount,
            'status': 'pending',
            'buyer_name': buyer_name,
            'buyer_email': buyer_email,
            'qris_amount': payload.get('amount') or '',
            'qris_expires': payload.get('countdown') or '',
            'qris_chat_id': qris_chat_id,
            'qris_message_id': qris_message_id,
            'next_check_at': next_check_at,
            'poll_attempts': 0,
            'last_polled_at': None,
            'referral_id': referral.get('id') if referral else None,
            'referrer_user_id': referral.get('referrer_user_id') if referral else None,
            'created_at': now,
            'updated_at': now,
        }
        query = self.client.table(self.table).insert(data)
        self._execute(query, 'create payment')

    def latest_pending_for_user(self, user_id, bot_code='default'):
        rows = []
        for status in ACTIVE_PAYMENT_STATUSES:
            query = (
                self.client.table(self.table)
                .select('*')
                .eq('user_id', user_id)
                .eq('bot_code', bot_code)
                .eq('status', status)
                .order('id', desc=True)
                .limit(1)
            )
            response = self._execute(query, f'latest active payment {status}')
            rows.extend(response.data or [])
        rows.sort(key=lambda item: item['id'], reverse=True)
        return rows[0] if rows else None

    def retryable_payments(self, due_before, limit):
        rows = []
        for status in RETRYABLE_PAYMENT_STATUSES:
            query = (
                self.client.table(self.table)
                .select('*')
                .eq('status', status)
                .lte('next_check_at', due_before)
                .order('next_check_at', desc=False)
                .order('id', desc=False)
                .limit(limit)
            )
            response = self._execute(query, f'retryable payments {status}')
            rows.extend(response.data or [])
        rows.sort(key=lambda item: ((item.get('next_check_at') or ''), item['id']))
        return rows[:limit]

    def recover_stale_processing(self, older_than_seconds=300):
        from vip_bot.helpers import utc_now_iso
        import datetime as dt
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=older_than_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        query = self.client.table(self.table).update(
            {
                'status': 'invite_error',
                'error': 'Recovered stale paid processing',
                'updated_at': now,
            }
        ).eq('status', 'processing_paid').lt('updated_at', cutoff)
        self._execute(query, 'recover stale processing_paid')

        query = self.client.table(self.table).update(
            {
                'status': 'delivery_error',
                'error': 'Recovered stale delivery processing',
                'updated_at': now,
            }
        ).eq('status', 'processing_delivery').lt('updated_at', cutoff)
        self._execute(query, 'recover stale processing_delivery')

    def claim_paid_processing(self, inv_id):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'processing_paid', 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).in_('status', ['pending', 'invite_error'])
        response = self._execute(query, 'claim paid processing')
        return bool(response.data)

    def mark_delivery_processing(self, inv_id, invite_link, invite_expires_at):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {
                'status': 'processing_delivery',
                'invite_link': invite_link,
                'invite_expires_at': invite_expires_at,
                'updated_at': utc_now_iso(),
            }
        ).eq('inv_id', inv_id).eq('status', 'processing_paid')
        response = self._execute(query, 'mark delivery processing')
        return bool(response.data)

    def mark_delivery_done(self, inv_id):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'paid', 'error': '', 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).in_('status', ['processing_delivery', 'delivery_error'])
        self._execute(query, 'mark delivery done')

    def mark_delivery_error(self, inv_id, error):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'delivery_error', 'error': str(error), 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).in_('status', ['processing_delivery', 'delivery_error'])
        self._execute(query, 'mark delivery error')

    def mark_delivery_blocked(self, inv_id, error):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'delivery_blocked', 'error': str(error), 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).in_('status', ['processing_delivery', 'delivery_error'])
        self._execute(query, 'mark delivery blocked')

    def mark_invite_error(self, inv_id, error):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'invite_error', 'error': str(error), 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).eq('status', 'processing_paid')
        self._execute(query, 'mark invite error')

    def mark_payment_pending(self, inv_id, attempts, next_check_at, error=''):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {
                'status': 'pending',
                'poll_attempts': attempts,
                'next_check_at': next_check_at,
                'last_polled_at': utc_now_iso(),
                'error': error or '',
                'updated_at': utc_now_iso(),
            }
        ).eq('inv_id', inv_id).eq('status', 'pending')
        self._execute(query, 'mark payment pending')

    def mark_payment_timeout(self, inv_id):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': 'timeout', 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).eq('status', 'pending')
        self._execute(query, 'mark payment timeout')

    def mark_payment_failed(self, inv_id, status_name, error):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.table).update(
            {'status': status_name, 'error': str(error), 'updated_at': utc_now_iso()}
        ).eq('inv_id', inv_id).eq('status', 'pending')
        self._execute(query, f'mark payment {status_name}')

    def ensure_payment_schema_ready(self):
        query = (
            self.client.table(self.table)
            .select('public_invoice_id,package_code,package_name,package_amount,vip_chat_id,invite_expire_hours,bot_code')
            .limit(1)
        )
        self._execute(query, 'ensure payment schema ready')

    # -------------------------------------------------------------------------
    # Package Management Methods (Scoped per bot_code)
    # -------------------------------------------------------------------------

    def list_packages(self, bot_code='default'):
        query = (
            self.client.table(self.package_table)
            .select('*')
            .eq('active', True)
            .eq('bot_code', bot_code)
            .order('sort_order', desc=False)
            .order('amount', desc=False)
        )
        response = self._execute(query, f'list packages {bot_code}')
        return response.data or []

    def list_all_packages(self, bot_code=None):
        query = self.client.table(self.package_table).select('*')
        if bot_code:
            query = query.eq('bot_code', bot_code)
        query = query.order('bot_code', desc=False).order('sort_order', desc=False)
        response = self._execute(query, 'list all packages')
        return response.data or []

    def get_package(self, code, bot_code='default'):
        from vip_bot.helpers import normalize_package_code
        query = (
            self.client.table(self.package_table)
            .select('*')
            .eq('code', normalize_package_code(code))
            .eq('bot_code', bot_code)
            .limit(1)
        )
        response = self._execute(query, 'get package')
        return response.data[0] if response.data else None

    def upsert_package(self, code, name, vip_chat_id, amount, invite_expire_hours=0, bot_code='default'):
        from vip_bot.helpers import utc_now_iso, normalize_package_code
        now = utc_now_iso()
        data = {
            'bot_code': bot_code,
            'code': normalize_package_code(code),
            'name': name.strip(),
            'vip_chat_id': int(vip_chat_id),
            'amount': int(amount),
            'invite_expire_hours': int(invite_expire_hours or 0),
            'active': True,
            'updated_at': now,
        }
        query = self.client.table(self.package_table).upsert(data, on_conflict='bot_code,code')
        self._execute(query, 'upsert package')

    def delete_package(self, code, bot_code='default'):
        from vip_bot.helpers import utc_now_iso, normalize_package_code
        query = self.client.table(self.package_table).update(
            {'active': False, 'updated_at': utc_now_iso()}
        ).eq('code', normalize_package_code(code)).eq('bot_code', bot_code)
        response = self._execute(query, 'delete package')
        return bool(response.data)

    # -------------------------------------------------------------------------
    # User & Referral Methods (Scoped per bot_code)
    # -------------------------------------------------------------------------

    def upsert_user(self, user, bot_code='default'):
        from vip_bot.helpers import utc_now_iso, format_referral_code, display_name
        existing = self.get_user(user.id, bot_code=bot_code)
        code = existing.get('referral_code') if existing else format_referral_code(user.id)
        data = {
            'bot_code': bot_code,
            'user_id': user.id,
            'username': user.username or '',
            'full_name': display_name(user),
            'referral_code': code,
            'is_bot': bool(getattr(user, 'bot', False)),
            'updated_at': utc_now_iso(),
        }
        if not existing:
            data.update(
                {
                    'balance': 0,
                    'pending_referrals': 0,
                    'successful_referrals': 0,
                    'created_at': utc_now_iso(),
                }
            )
        response = self._execute(
            self.client.table(self.user_table).upsert(data, on_conflict='bot_code,user_id'),
            'upsert user',
        )
        return response.data[0] if response.data else {**(existing or {}), **data}

    def get_user(self, user_id, bot_code='default'):
        query = (
            self.client.table(self.user_table)
            .select('*')
            .eq('user_id', int(user_id))
            .eq('bot_code', bot_code)
            .limit(1)
        )
        response = self._execute(query, 'get user')
        return response.data[0] if response.data else None

    def get_user_by_referral_code(self, code, bot_code='default'):
        query = (
            self.client.table(self.user_table)
            .select('*')
            .eq('referral_code', code)
            .eq('bot_code', bot_code)
            .limit(1)
        )
        response = self._execute(query, 'get user by referral code')
        return response.data[0] if response.data else None

    def create_referral_if_absent(self, referrer, invited_user, bot_code='default'):
        from vip_bot.helpers import utc_now_iso, display_name, should_create_referral
        invited = self.upsert_user(invited_user, bot_code=bot_code)
        if not should_create_referral(
            invited_user.id, referrer.get('user_id') if referrer else 0, invited.get('invited_by_user_id')
        ):
            return None, False
        code = referrer['referral_code']
        data = {
            'bot_code': bot_code,
            'referrer_user_id': int(referrer['user_id']),
            'referrer_code': code,
            'invited_user_id': invited_user.id,
            'invited_username': invited_user.username or '',
            'invited_full_name': display_name(invited_user),
            'status': 'pending',
            'created_at': utc_now_iso(),
            'updated_at': utc_now_iso(),
        }
        response = self._execute(self.client.table(self.referral_table).insert(data), 'create referral')
        referral = response.data[0] if response.data else data
        self._execute(
            self.client.table(self.user_table)
            .update({'invited_by_user_id': int(referrer['user_id']), 'updated_at': utc_now_iso()})
            .eq('user_id', invited_user.id)
            .eq('bot_code', bot_code)
            .is_('invited_by_user_id', 'null'),
            'set invited by user',
        )
        self._execute(
            self.client.rpc(
                'vip_increment_pending_referral',
                {'p_user_id': int(referrer['user_id']), 'p_bot_code': bot_code},
            ),
            'increment pending referral',
        )
        return referral, True

    def pending_referral_for_user(self, user_id, bot_code='default'):
        query = (
            self.client.table(self.referral_table)
            .select('*')
            .eq('invited_user_id', user_id)
            .eq('bot_code', bot_code)
            .eq('status', 'pending')
            .limit(1)
        )
        response = self._execute(query, 'get pending referral')
        return response.data[0] if response.data else None

    def referral_stats(self, user_id, bot_code='default'):
        from vip_bot.helpers import format_referral_code
        user = self.get_user(user_id, bot_code=bot_code) or {}
        return {
            'pending_count': int(user.get('pending_referrals') or 0),
            'successful_count': int(user.get('successful_referrals') or 0),
            'balance': int(user.get('balance') or 0),
            'referral_code': user.get('referral_code') or format_referral_code(user_id),
            'invited_by_user_id': user.get('invited_by_user_id'),
            'phone': user.get('phone') or '',
        }

    def mark_referral_paid(self, referral_id, payment, commission):
        from vip_bot.helpers import utc_now_iso
        bot_code = payment.get('bot_code') or 'default'
        query = self.client.table(self.referral_table).update(
            {
                'status': 'paid',
                'payment_inv_id': payment['inv_id'],
                'package_code': payment.get('package_code') or '',
                'package_amount': int(payment.get('package_amount') or 0),
                'commission_amount': int(commission),
                'updated_at': utc_now_iso(),
            }
        ).eq('id', referral_id).eq('status', 'pending')
        response = self._execute(query, 'mark referral paid')
        referral = response.data[0] if response.data else None
        if referral:
            self._execute(
                self.client.rpc(
                    'vip_credit_referral_commission',
                    {
                        'p_user_id': int(referral['referrer_user_id']),
                        'p_amount': int(commission),
                        'p_bot_code': bot_code,
                    },
                ),
                'credit referral balance',
            )
        return referral

    # -------------------------------------------------------------------------
    # Withdrawal Methods (Scoped per bot_code)
    # -------------------------------------------------------------------------

    def create_withdrawal(self, user, amount, details, bot_code='default'):
        from vip_bot.helpers import display_name
        response = self._execute(
            self.client.rpc(
                'vip_create_withdrawal',
                {
                    'p_user_id': user.id,
                    'p_username': user.username or '',
                    'p_full_name': display_name(user),
                    'p_amount': int(amount),
                    'p_phone': details['phone'],
                    'p_wallet_name': details['wallet_name'],
                    'p_account_name': details['account_name'],
                    'p_bot_code': bot_code,
                },
            ),
            'create withdrawal',
        )
        if not response.data:
            raise ValueError('Insufficient balance')
        return response.data[0]

    def get_withdrawal(self, withdrawal_id):
        query = self.client.table(self.withdrawal_table).select('*').eq('id', int(withdrawal_id)).limit(1)
        response = self._execute(query, 'get withdrawal')
        return response.data[0] if response.data else None

    def update_withdrawal_status(self, withdrawal_id, from_status, to_status, admin_user_id):
        from vip_bot.helpers import utc_now_iso
        query = self.client.table(self.withdrawal_table).update(
            {'status': to_status, 'admin_user_id': int(admin_user_id), 'updated_at': utc_now_iso()}
        ).eq('id', int(withdrawal_id)).eq('status', from_status)
        response = self._execute(query, f'mark withdrawal {to_status}')
        withdrawal = response.data[0] if response.data else None
        if withdrawal and to_status == 'rejected':
            bot_code = withdrawal.get('bot_code') or 'default'
            self._execute(
                self.client.rpc(
                    'vip_credit_balance',
                    {
                        'p_user_id': int(withdrawal['user_id']),
                        'p_amount': int(withdrawal.get('amount') or 0),
                        'p_bot_code': bot_code,
                    },
                ),
                'refund rejected withdrawal',
            )
        return withdrawal

    # -------------------------------------------------------------------------
    # Settings & Broadcast Methods
    # -------------------------------------------------------------------------

    def get_setting(self, key, default=''):
        query = self.client.table(self.settings_table).select('value').eq('key', key).limit(1)
        response = self._execute(query, f'get setting {key}')
        return response.data[0]['value'] if response.data else default

    def set_setting(self, key, value):
        from vip_bot.helpers import utc_now_iso
        data = {'key': key, 'value': str(value), 'updated_at': utc_now_iso()}
        query = self.client.table(self.settings_table).upsert(data, on_conflict='key')
        self._execute(query, f'set setting {key}')

    def get_active_broadcast_message(self):
        query = (
            self.client.table(self.broadcast_table)
            .select('*')
            .eq('is_active', True)
            .order('id', desc=True)
            .limit(1)
        )
        response = self._execute(query, 'get active broadcast message')
        return response.data[0] if response.data else None

    def set_broadcast_message(self, message_text, media_file_id, media_type, entities_json):
        from vip_bot.helpers import utc_now_iso
        now = utc_now_iso()
        self._execute(
            self.client.table(self.broadcast_table).update({'is_active': False, 'updated_at': now}),
            'disable previous broadcast',
        )
        data = {
            'message_text': message_text or '',
            'media_telegram_file_id': media_file_id or '',
            'media_type': media_type or '',
            'entities_json': entities_json or '',
            'is_active': True,
            'created_at': now,
            'updated_at': now,
        }
        response = self._execute(self.client.table(self.broadcast_table).insert(data), 'set broadcast message')
        return response.data[0] if response.data else data

    def get_broadcast_targets(self, before_iso=None, limit=20, bot_code=None):
        query = self.client.table(self.user_table).select('user_id').eq('is_bot', False)
        if bot_code:
            query = query.eq('bot_code', bot_code)
        if before_iso:
            query = query.or_(f'last_broadcast_at.is.null,last_broadcast_at.lt.{before_iso}')
        response = self._execute(
            query.order('last_broadcast_at', desc=False, nullsfirst=True)
            .order('user_id', desc=False)
            .limit(max(1, int(limit))),
            'get broadcast targets',
        )
        return response.data or []

    def mark_user_broadcasted(self, user_id, bot_code='default'):
        from vip_bot.helpers import utc_now_iso
        self._execute(
            self.client.table(self.user_table).update(
                {'last_broadcast_at': utc_now_iso(), 'updated_at': utc_now_iso()}
            ).eq('user_id', int(user_id)).eq('bot_code', bot_code),
            'mark user broadcasted',
        )

    def count_broadcast_targets(self, bot_code=None):
        query = self.client.table(self.user_table).select('user_id', count='exact').eq('is_bot', False)
        if bot_code:
            query = query.eq('bot_code', bot_code)
        response = self._execute(query.limit(1), 'count broadcast targets')
        return int(response.count or 0)

    def set_broadcast_time(self, time_str):
        self.set_setting('broadcast_time', time_str or '')

    def get_broadcast_time(self):
        return self.get_setting('broadcast_time', '')

    def set_last_broadcast_date(self, date_str):
        self.set_setting('last_broadcast_date', date_str or '')

    def get_last_broadcast_date(self):
        return self.get_setting('last_broadcast_date', '')
