-- Scale existing affection scores from 0-100 to 0-1000
UPDATE companion_affection SET score = score * 10 WHERE score <= 100;

-- Add read_at timestamp to messages for read receipts
ALTER TABLE companion_messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMP;
