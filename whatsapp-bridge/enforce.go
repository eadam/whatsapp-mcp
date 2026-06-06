package main

// Homelab send-enforcement layer.
//
// This file is part of the eadam/whatsapp-mcp `homelab` hardening branch. It is
// NOT in the verygoodplugins upstream. The goal: the /api/send endpoint is the
// only path that can deliver a WhatsApp message, so all outbound controls live
// here at that chokepoint, behind the bridge's existing bearer-token + loopback
// Host gate (auth.go). Even a fully prompt-injected MCP agent cannot escape
// these, because the MCP server's only send path is this REST endpoint.
//
// Controls (see ~/.claude plan "Add a WhatsApp MCP to the homelab", Phase 1):
//   1. Atomic, serialized enforcement — one process mutex + one DB transaction
//      reserve a slot before the network send; concurrent calls can't both pass
//      a cap check before either writes.
//   2. Strict E.164 allowlist, fail-closed — empty list denies all; groups,
//      @lid, broadcast/newsletter, and malformed JIDs are rejected.
//   3. Daily cap + per-recipient/day cap (counters keyed by local date; the
//      container runs TZ=America/Phoenix).
//   4. Duplicate suppression with no automatic ambiguous retry — a campaign's
//      (campaign_id, recipient, message_hash) is reserved `pending` before the
//      send and transitioned to `sent`/`failed`. A crash leaves `pending`,
//      which becomes `unknown` on the next boot and is NEVER auto-retried.
//      WhatsApp gives no transactional idempotency contract, so this is
//      best-effort suppression, not guaranteed exactly-once delivery.
//   5. Approved-manifest binding — in campaign mode the bridge sends ONLY
//      (recipient, message_hash) pairs an operator pre-loaded via the
//      `-load-manifest` admin invocation (never reachable through MCP).
//   6. Rollback-safe audit — every decision is written in its own autocommit
//      statement, after the reservation transaction commits or rolls back, so a
//      rejected/rolled-back attempt still leaves an audit trail.

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// sendMu serializes reservations in-process. The bridge is single-process, so a
// mutex fully orders the reserve transactions and avoids SQLITE_BUSY races
// between concurrent /api/send calls. It is held only for the (fast, local) DB
// reservation — never across the network send.
var sendMu sync.Mutex

// guardConfig is the static, env-derived send policy, loaded once at startup.
type guardConfig struct {
	allowed     map[string]bool // canonical "<digits>@s.whatsapp.net"
	campaignID  string          // "" = ad-hoc mode (no manifest binding, no dedup)
	maxPerDay   int
	maxPerRecip int
}

// guardDecision is the outcome of a reservation attempt.
type guardDecision int

const (
	decAllow     guardDecision = iota // proceed to send, then finalize
	decDuplicate                      // already sent/in-flight; no-op success
	decReject                         // do not send; return the reason
)

func getEnvInt(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return def
	}
	return n
}

// loadGuardConfig reads the policy from the environment. Malformed allowlist
// entries are skipped with a warning rather than crashing the bridge.
func loadGuardConfig() guardConfig {
	cfg := guardConfig{
		allowed:     map[string]bool{},
		campaignID:  strings.TrimSpace(os.Getenv("WHATSAPP_CAMPAIGN_ID")),
		maxPerDay:   getEnvInt("WHATSAPP_MAX_SENDS_PER_DAY", 5),
		maxPerRecip: getEnvInt("WHATSAPP_MAX_PER_RECIPIENT_PER_DAY", 1),
	}
	for _, raw := range strings.Split(os.Getenv("WHATSAPP_ALLOWED_RECIPIENTS"), ",") {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		canon, err := normalizeRecipient(raw)
		if err != nil {
			fmt.Printf("guard: skipping invalid allowlist entry %q: %v\n", raw, err)
			continue
		}
		cfg.allowed[canon] = true
	}
	return cfg
}

func (c guardConfig) logBanner() {
	mode := "ad-hoc (no manifest binding)"
	if c.campaignID != "" {
		mode = fmt.Sprintf("campaign %q (manifest-bound)", c.campaignID)
	}
	fmt.Printf("guard: send policy — mode=%s allowlist=%d recipient(s) max/day=%d max/recipient/day=%d\n",
		mode, len(c.allowed), c.maxPerDay, c.maxPerRecip)
	if len(c.allowed) == 0 {
		fmt.Printf("guard: WARNING allowlist is EMPTY — all sends will be denied (fail-closed)\n")
	}
}

// normalizeRecipient enforces "strict E.164 direct recipient only" and returns
// the canonical "<digits>@s.whatsapp.net" form used everywhere downstream.
// Rejects groups (@g.us), @lid, broadcast/newsletter/status, and anything whose
// user part is not a plain digit string — closing allowlist-bypass via
// alternate address representations.
func normalizeRecipient(recipient string) (string, error) {
	r := strings.TrimSpace(recipient)
	if r == "" {
		return "", fmt.Errorf("empty recipient")
	}
	digits := r
	if i := strings.IndexByte(r, '@'); i >= 0 {
		server := strings.ToLower(r[i+1:])
		if server != "s.whatsapp.net" {
			return "", fmt.Errorf("non-personal JID server %q (groups/@lid/broadcast not allowed)", server)
		}
		digits = r[:i]
	}
	digits = strings.TrimPrefix(digits, "+")
	// Strip a device/agent suffix like ":12" if present; the user part must
	// still be all digits afterward.
	if i := strings.IndexByte(digits, ':'); i >= 0 {
		digits = digits[:i]
	}
	if digits == "" {
		return "", fmt.Errorf("no digits in recipient %q", recipient)
	}
	for _, ch := range digits {
		if ch < '0' || ch > '9' {
			return "", fmt.Errorf("recipient %q is not strict E.164 (non-digit %q)", recipient, string(ch))
		}
	}
	// E.164 allows up to 15 digits; require a sane minimum to reject junk.
	if len(digits) < 6 || len(digits) > 15 {
		return "", fmt.Errorf("recipient %q has implausible length %d (expected 6-15 digits)", recipient, len(digits))
	}
	return digits + "@s.whatsapp.net", nil
}

// messageHash is the content identity used for dedup and manifest binding.
func messageHash(message, mediaPath string) string {
	sum := sha256.Sum256([]byte(message + "\x00" + mediaPath))
	return hex.EncodeToString(sum[:])
}

func localDayKey() string {
	return time.Now().Format("2006-01-02")
}

// ensureGuardSchema creates the enforcement tables. Idempotent.
func ensureGuardSchema(db *sql.DB) error {
	_, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS send_ledger (
			id           INTEGER PRIMARY KEY AUTOINCREMENT,
			campaign_id  TEXT NOT NULL,
			recipient    TEXT NOT NULL,
			message_hash TEXT NOT NULL,
			day_key      TEXT NOT NULL,
			state        TEXT NOT NULL,          -- pending|sent|failed|unknown
			created_at   INTEGER NOT NULL,
			updated_at   INTEGER NOT NULL
		);
		CREATE INDEX IF NOT EXISTS idx_ledger_day        ON send_ledger(day_key, state);
		CREATE INDEX IF NOT EXISTS idx_ledger_recip_day  ON send_ledger(recipient, day_key, state);
		CREATE INDEX IF NOT EXISTS idx_ledger_dedup      ON send_ledger(campaign_id, recipient, message_hash, state);

		CREATE TABLE IF NOT EXISTS send_manifest (
			campaign_id  TEXT NOT NULL,
			recipient    TEXT NOT NULL,
			message_hash TEXT NOT NULL,
			created_at   INTEGER NOT NULL,
			PRIMARY KEY (campaign_id, recipient, message_hash)
		);

		CREATE TABLE IF NOT EXISTS send_audit (
			id           INTEGER PRIMARY KEY AUTOINCREMENT,
			ts           INTEGER NOT NULL,
			campaign_id  TEXT,
			recipient    TEXT,
			message_hash TEXT,
			decision     TEXT NOT NULL,
			detail       TEXT
		);
		CREATE INDEX IF NOT EXISTS idx_audit_ts ON send_audit(ts);
	`)
	if err != nil {
		return fmt.Errorf("failed to create guard schema: %v", err)
	}
	return nil
}

// recoverStalePending runs once at startup: any `pending` row is the residue of
// a crash mid-send. We promote it to `unknown` and NEVER auto-retry — a human
// resolves it (the message may or may not have been delivered). This both
// prevents accidental duplicate retries and keeps the row counting against caps.
func recoverStalePending(db *sql.DB) error {
	now := time.Now().Unix()
	res, err := db.Exec(`UPDATE send_ledger SET state='unknown', updated_at=? WHERE state='pending'`, now)
	if err != nil {
		return fmt.Errorf("failed to recover stale pending sends: %v", err)
	}
	n, _ := res.RowsAffected()
	if n > 0 {
		fmt.Printf("guard: recovered %d stale pending send(s) to state=unknown (will not auto-retry)\n", n)
		// Separate autocommit audit write.
		_, _ = db.Exec(`INSERT INTO send_audit(ts, decision, detail) VALUES (?, 'recovered-unknown', ?)`,
			now, fmt.Sprintf("%d row(s) promoted pending->unknown on startup", n))
	}
	return nil
}

// audit writes a single decision row in its own autocommit transaction. Called
// AFTER the reservation transaction has committed or rolled back, so a rejected
// or rolled-back attempt still leaves a durable trail.
func audit(db *sql.DB, campaignID, recipient, msgHash, decision, detail string) {
	_, err := db.Exec(
		`INSERT INTO send_audit(ts, campaign_id, recipient, message_hash, decision, detail) VALUES (?,?,?,?,?,?)`,
		time.Now().Unix(), campaignID, recipient, msgHash, decision, detail)
	if err != nil {
		fmt.Printf("guard: WARNING failed to write audit row (%s): %v\n", decision, err)
	}
}

// reserveSend evaluates allowlist + manifest + dedup + caps and, on allow,
// reserves a `pending` ledger row — all inside one serialized transaction.
// Returns (decision, ledgerID, humanReason).
//
//   - decAllow:     ledgerID is the row to finalize after the network send.
//   - decDuplicate: a prior sent/in-flight send for this exact tuple exists; the
//     caller returns success WITHOUT sending.
//   - decReject:    do not send; humanReason explains why.
func reserveSend(db *sql.DB, cfg guardConfig, recipient, msgHash string) (guardDecision, int64, string) {
	sendMu.Lock()
	defer sendMu.Unlock()

	day := localDayKey()
	now := time.Now().Unix()

	// Evaluate-and-reserve atomically.
	tx, err := db.Begin()
	if err != nil {
		audit(db, cfg.campaignID, recipient, msgHash, "reject", "tx-begin-failed")
		return decReject, 0, "internal error (tx begin)"
	}
	rollback := func(decision, reason string) (guardDecision, int64, string) {
		_ = tx.Rollback()
		audit(db, cfg.campaignID, recipient, msgHash, decision, reason)
		return decReject, 0, reason
	}

	// 1. Allowlist (fail-closed: empty allowlist denies all).
	if !cfg.allowed[recipient] {
		return rollback("reject", "recipient not on allowlist")
	}

	// 2. Manifest binding (campaign mode only).
	if cfg.campaignID != "" {
		var n int
		if err := tx.QueryRow(
			`SELECT COUNT(1) FROM send_manifest WHERE campaign_id=? AND recipient=? AND message_hash=?`,
			cfg.campaignID, recipient, msgHash).Scan(&n); err != nil {
			return rollback("reject", "manifest lookup failed")
		}
		if n == 0 {
			return rollback("reject", "off-manifest (recipient/message not in approved campaign manifest)")
		}

		// 3. Duplicate suppression (campaign mode). Inspect prior states for this tuple.
		var prior string
		err := tx.QueryRow(
			`SELECT state FROM send_ledger
			 WHERE campaign_id=? AND recipient=? AND message_hash=?
			 ORDER BY CASE state WHEN 'sent' THEN 0 WHEN 'pending' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END
			 LIMIT 1`,
			cfg.campaignID, recipient, msgHash).Scan(&prior)
		switch {
		case err == sql.ErrNoRows:
			// fresh — fall through to caps + reserve
		case err != nil:
			return rollback("reject", "ledger dedup lookup failed")
		case prior == "sent" || prior == "pending":
			_ = tx.Rollback()
			audit(db, cfg.campaignID, recipient, msgHash, "duplicate-suppressed", "prior state="+prior)
			return decDuplicate, 0, "duplicate suppressed (already " + prior + ")"
		case prior == "unknown":
			return rollback("reject", "prior attempt in unknown state — manual resolution required (no auto-retry)")
			// prior == "failed" falls through: a definite failure may be retried.
		}
	}

	// 4. Caps. pending/sent/unknown count; definite failed does not.
	var globalCount, recipCount int
	if err := tx.QueryRow(
		`SELECT COUNT(1) FROM send_ledger WHERE day_key=? AND state IN ('pending','sent','unknown')`,
		day).Scan(&globalCount); err != nil {
		return rollback("reject", "daily cap lookup failed")
	}
	if globalCount >= cfg.maxPerDay {
		return rollback("reject", fmt.Sprintf("daily send cap reached (%d/%d)", globalCount, cfg.maxPerDay))
	}
	if err := tx.QueryRow(
		`SELECT COUNT(1) FROM send_ledger WHERE recipient=? AND day_key=? AND state IN ('pending','sent','unknown')`,
		recipient, day).Scan(&recipCount); err != nil {
		return rollback("reject", "per-recipient cap lookup failed")
	}
	if recipCount >= cfg.maxPerRecip {
		return rollback("reject", fmt.Sprintf("per-recipient daily cap reached (%d/%d)", recipCount, cfg.maxPerRecip))
	}

	// 5. Reserve.
	res, err := tx.Exec(
		`INSERT INTO send_ledger(campaign_id, recipient, message_hash, day_key, state, created_at, updated_at)
		 VALUES (?,?,?,?,'pending',?,?)`,
		cfg.campaignID, recipient, msgHash, day, now, now)
	if err != nil {
		return rollback("reject", "ledger reservation insert failed")
	}
	id, _ := res.LastInsertId()
	if err := tx.Commit(); err != nil {
		audit(db, cfg.campaignID, recipient, msgHash, "reject", "tx-commit-failed")
		return decReject, 0, "internal error (tx commit)"
	}
	audit(db, cfg.campaignID, recipient, msgHash, "allow", fmt.Sprintf("reserved ledger id=%d", id))
	return decAllow, id, ""
}

// finalizeSend transitions a reserved row to sent/failed after the network call.
func finalizeSend(db *sql.DB, ledgerID int64, success bool) {
	state := "failed"
	if success {
		state = "sent"
	}
	if _, err := db.Exec(`UPDATE send_ledger SET state=?, updated_at=? WHERE id=?`,
		state, time.Now().Unix(), ledgerID); err != nil {
		fmt.Printf("guard: WARNING failed to finalize ledger id=%d -> %s: %v\n", ledgerID, state, err)
	}
	_, _ = db.Exec(`INSERT INTO send_audit(ts, decision, detail) VALUES (?,?,?)`,
		time.Now().Unix(), state, fmt.Sprintf("ledger id=%d", ledgerID))
}
