-- FDE Support Ticket Router Database Schema
-- Matches Technical Design Document (jliu_support_tdd.pdf Page 4-5)

-- Enable UUID extension if using PostgreSQL
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Tickets Table
CREATE TABLE IF NOT EXISTS tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id VARCHAR(255) NOT NULL,
    current_status VARCHAR(50) NOT NULL DEFAULT 'NEW', -- NEW, ESCALATED, RESOLVED
    sentiment VARCHAR(50) NOT NULL DEFAULT 'NEUTRAL_FACTUAL', -- NEUTRAL_FACTUAL, URGENT_FRUSTRATED
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Messages Table
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender VARCHAR(50) NOT NULL, -- CUSTOMER, AGENT, SYSTEM
    message_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_tickets_customer_id ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(current_status);
CREATE INDEX IF NOT EXISTS idx_messages_ticket_id ON messages(ticket_id);
