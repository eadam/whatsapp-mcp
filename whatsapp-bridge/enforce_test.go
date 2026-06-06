package main

import (
	"database/sql"
	"path/filepath"
	"sync"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func TestNormalizeRecipient(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{"+15551234567", "15551234567@s.whatsapp.net", false},
		{"15551234567", "15551234567@s.whatsapp.net", false},
		{" 15551234567 ", "15551234567@s.whatsapp.net", false},
		{"15551234567@s.whatsapp.net", "15551234567@s.whatsapp.net", false},
		{"15551234567:12@s.whatsapp.net", "15551234567@s.whatsapp.net", false}, // device suffix stripped
		{"120363000000000000@g.us", "", true},                                  // group
		{"15551234567@lid", "", true},                                          // LID
		{"status@broadcast", "", true},                                         // broadcast
		{"not-a-number", "", true},
		{"1555ABC4567", "", true}, // letters
		{"12345", "", true},       // too short
		{"", "", true},
	}
	for _, c := range cases {
		got, err := normalizeRecipient(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("normalizeRecipient(%q) = %q, want error", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("normalizeRecipient(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("normalizeRecipient(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "messages.db")
	db, err := sql.Open("sqlite3", "file:"+path+"?_foreign_keys=on")
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := ensureGuardSchema(db); err != nil {
		t.Fatalf("schema: %v", err)
	}
	return db
}

const rcpt = "15551234567@s.whatsapp.net"

func TestReserveSend_FailClosedAllowlist(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{}, maxPerDay: 100, maxPerRecip: 100} // empty allowlist
	dec, _, reason := reserveSend(db, cfg, rcpt, "h1")
	if dec != decReject {
		t.Fatalf("empty allowlist should reject, got dec=%d reason=%q", dec, reason)
	}
}

func TestReserveSend_PerRecipientCap(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{rcpt: true}, maxPerDay: 100, maxPerRecip: 1}
	if dec, id, _ := reserveSend(db, cfg, rcpt, "h1"); dec != decAllow {
		t.Fatalf("first send should be allowed, got %d", dec)
	} else {
		finalizeSend(db, id, true)
	}
	if dec, _, _ := reserveSend(db, cfg, rcpt, "h2"); dec != decReject {
		t.Fatalf("second send to same recipient should hit per-recipient cap, got %d", dec)
	}
}

func TestReserveSend_DailyCap(t *testing.T) {
	db := newTestDB(t)
	r2 := "15557654321@s.whatsapp.net"
	cfg := guardConfig{allowed: map[string]bool{rcpt: true, r2: true}, maxPerDay: 1, maxPerRecip: 5}
	if dec, id, _ := reserveSend(db, cfg, rcpt, "h1"); dec != decAllow {
		t.Fatalf("first send should be allowed, got %d", dec)
	} else {
		finalizeSend(db, id, true)
	}
	if dec, _, _ := reserveSend(db, cfg, r2, "h1"); dec != decReject {
		t.Fatalf("second send (different recipient) should hit global daily cap, got %d", dec)
	}
}

func TestReserveSend_FailedDoesNotCountAgainstCap(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{rcpt: true}, maxPerDay: 100, maxPerRecip: 1}
	dec, id, _ := reserveSend(db, cfg, rcpt, "h1")
	if dec != decAllow {
		t.Fatalf("first reserve should allow, got %d", dec)
	}
	finalizeSend(db, id, false) // definite failure
	// A definite failed send must free the slot so a retry is possible.
	if dec, _, reason := reserveSend(db, cfg, rcpt, "h2"); dec != decAllow {
		t.Fatalf("after a failed send the per-recipient slot should be free, got dec=%d reason=%q", dec, reason)
	}
}

func TestReserveSend_CampaignManifestAndDedup(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{rcpt: true}, campaignID: "c1", maxPerDay: 100, maxPerRecip: 100}

	// Off-manifest content is rejected even though the recipient is allowlisted.
	if dec, _, _ := reserveSend(db, cfg, rcpt, "hX"); dec != decReject {
		t.Fatalf("off-manifest send should reject, got %d", dec)
	}

	// Load the manifest pair, then it's allowed once.
	if _, err := db.Exec(`INSERT INTO send_manifest(campaign_id, recipient, message_hash, created_at) VALUES ('c1',?,?,0)`, rcpt, "hOK"); err != nil {
		t.Fatal(err)
	}
	dec, id, _ := reserveSend(db, cfg, rcpt, "hOK")
	if dec != decAllow {
		t.Fatalf("on-manifest send should allow, got %d", dec)
	}
	finalizeSend(db, id, true)

	// Re-sending the same (campaign, recipient, hash) is suppressed.
	if dec, _, _ := reserveSend(db, cfg, rcpt, "hOK"); dec != decDuplicate {
		t.Fatalf("re-send of sent pair should be duplicate-suppressed, got %d", dec)
	}
}

func TestReserveSend_UnknownNotRetried(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{rcpt: true}, campaignID: "c1", maxPerDay: 100, maxPerRecip: 100}
	if _, err := db.Exec(`INSERT INTO send_manifest(campaign_id, recipient, message_hash, created_at) VALUES ('c1',?,?,0)`, rcpt, "hOK"); err != nil {
		t.Fatal(err)
	}
	// Simulate a crash residue: a pending row promoted to unknown.
	if _, err := db.Exec(`INSERT INTO send_ledger(campaign_id,recipient,message_hash,day_key,state,created_at,updated_at) VALUES ('c1',?,?,?,'unknown',0,0)`, rcpt, "hOK", localDayKey()); err != nil {
		t.Fatal(err)
	}
	if dec, _, reason := reserveSend(db, cfg, rcpt, "hOK"); dec != decReject {
		t.Fatalf("unknown prior state must NOT auto-retry, got dec=%d reason=%q", dec, reason)
	}
}

func TestRecoverStalePending(t *testing.T) {
	db := newTestDB(t)
	if _, err := db.Exec(`INSERT INTO send_ledger(campaign_id,recipient,message_hash,day_key,state,created_at,updated_at) VALUES ('c1',?,?,?,'pending',0,0)`, rcpt, "h1", localDayKey()); err != nil {
		t.Fatal(err)
	}
	if err := recoverStalePending(db); err != nil {
		t.Fatal(err)
	}
	var state string
	if err := db.QueryRow(`SELECT state FROM send_ledger WHERE message_hash='h1'`).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "unknown" {
		t.Fatalf("stale pending should become unknown, got %q", state)
	}
}

func TestReserveSend_ConcurrencyRespectsCap(t *testing.T) {
	db := newTestDB(t)
	cfg := guardConfig{allowed: map[string]bool{rcpt: true}, maxPerDay: 3, maxPerRecip: 100}
	var wg sync.WaitGroup
	var mu sync.Mutex
	allowed := 0
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			// Distinct hashes so dedup never collapses them; only caps gate.
			dec, id, _ := reserveSend(db, cfg, rcpt, "h"+string(rune('a'+i)))
			if dec == decAllow {
				mu.Lock()
				allowed++
				mu.Unlock()
				finalizeSend(db, id, true)
			}
		}(i)
	}
	wg.Wait()
	if allowed != cfg.maxPerDay {
		t.Fatalf("concurrent reservations should allow exactly the daily cap (%d), got %d", cfg.maxPerDay, allowed)
	}
}
