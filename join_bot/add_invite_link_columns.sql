-- ============================================================
-- Migration: Add columns to support Auto-Fetched Telegram Invite Links
-- Run this in your Supabase SQL Editor
-- ============================================================

-- 1. Add columns to tg_bot_join_links for storing Telegram-native invite link details
ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS invite_link TEXT DEFAULT NULL;

ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS is_auto_fetched BOOLEAN DEFAULT FALSE;

ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS telegram_admin_id TEXT DEFAULT NULL;

ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS is_request_needed BOOLEAN DEFAULT FALSE;

ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS expire_date TIMESTAMP WITH TIME ZONE DEFAULT NULL;

ALTER TABLE tg_bot_join_links
ADD COLUMN IF NOT EXISTS usage_limit INT DEFAULT NULL;

-- 2. Add sync trigger flag in tg_bot_channel_mappings
ALTER TABLE tg_bot_channel_mappings
ADD COLUMN IF NOT EXISTS last_links_synced_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;

ALTER TABLE tg_bot_channel_mappings
ADD COLUMN IF NOT EXISTS sync_links_requested BOOLEAN DEFAULT FALSE;

-- 3. Add index for faster link matching
CREATE INDEX IF NOT EXISTS idx_tg_bot_join_links_invite_url
ON tg_bot_join_links (invite_link);

CREATE INDEX IF NOT EXISTS idx_tg_bot_join_links_channel_mapping
ON tg_bot_join_links (channel_mapping_id);
