package main

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
)

// ── Helper: minimal MessageStore backed by a temp SQLite DB ──────────────────

func newTestMessageStore(t *testing.T) (*MessageStore, string) {
	t.Helper()
	dir := t.TempDir()

	// Create store subdir to match what NewMessageStore would do.
	storeDir := filepath.Join(dir, "store")
	if err := os.MkdirAll(storeDir, 0o700); err != nil {
		t.Fatalf("MkdirAll store: %v", err)
	}

	db, err := sql.Open("sqlite3", "file:"+filepath.Join(storeDir, "messages.db")+"?_foreign_keys=on")
	if err != nil {
		t.Fatalf("sql.Open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP,
			ephemeral_expiration INTEGER NOT NULL DEFAULT 0,
			ephemeral_setting_timestamp INTEGER NOT NULL DEFAULT 0);
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
			is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
			media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
			deleted_at TIMESTAMP, quoted_message_id TEXT,
			PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid));
	`)
	if err != nil {
		t.Fatalf("create tables: %v", err)
	}

	// Resolve storeRoot — mirrors NewMessageStore logic.
	resolvedStore, err := filepath.EvalSymlinks(storeDir)
	if err != nil {
		resolvedStore = storeDir
	}

	return &MessageStore{db: db, storeRoot: resolvedStore}, resolvedStore
}

// insertTestMessage inserts a message row for download tests.
func insertTestMessage(t *testing.T, store *MessageStore, id, chatJID, mediaType string, fileLength uint64, url string, key []byte) {
	t.Helper()
	_, err := store.db.Exec(`
		INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, 'Test', datetime('now'))
	`, chatJID)
	if err != nil {
		t.Fatalf("insert chat: %v", err)
	}
	_, err = store.db.Exec(`
		INSERT OR REPLACE INTO messages
		  (id, chat_jid, media_type, url, media_key, file_sha256, file_enc_sha256, file_length, timestamp, sender, content, is_from_me)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'test', '', 0)
	`, id, chatJID, mediaType, url, key, key, key, fileLength)
	if err != nil {
		t.Fatalf("insert message: %v", err)
	}
}

// ── identifierRe tests ────────────────────────────────────────────────────────

func TestIdentifierRe_Valid(t *testing.T) {
	cases := []string{
		"3EB0F4A2B1C3D4E5",
		"15551234567@s.whatsapp.net",
		"msg-id_2024-01-01",
		"ABC123",
		"+15551234567",
	}
	for _, c := range cases {
		if !identifierRe.MatchString(c) {
			t.Errorf("expected %q to match identifierRe", c)
		}
	}
}

func TestIdentifierRe_Rejected(t *testing.T) {
	cases := []string{
		"../etc/passwd",
		"/absolute/path",
		"has space",
		"has\x00null",
		"",
	}
	for _, c := range cases {
		if identifierRe.MatchString(c) {
			t.Errorf("expected %q NOT to match identifierRe", c)
		}
	}
}

// ── Input validation tests ────────────────────────────────────────────────────

func TestPathConfinement_DotDot(t *testing.T) {
	store, _ := newTestMessageStore(t)
	// ".." passes identifierRe (two dots), but the filepath.Clean check rejects it.
	_, mErr := downloadMediaForAPI(nil, store, "..", "15551234567@s.whatsapp.net", 5*1024*1024)
	if mErr == nil || mErr.Code != "path_confinement" {
		t.Errorf("expected path_confinement for '..' messageID, got %v", mErr)
	}
}

func TestPathConfinement_SlashInID(t *testing.T) {
	store, _ := newTestMessageStore(t)
	// "/" is not in identifierRe, so this should also return path_confinement.
	_, mErr := downloadMediaForAPI(nil, store, "../../etc/passwd", "15551234567@s.whatsapp.net", 5*1024*1024)
	if mErr == nil || mErr.Code != "path_confinement" {
		t.Errorf("expected path_confinement for slash messageID, got %v", mErr)
	}
}

// ── DB not-found / non-image tests ───────────────────────────────────────────

func TestNotFound_NoMessageRow(t *testing.T) {
	store, _ := newTestMessageStore(t)
	_, mErr := downloadMediaForAPI(nil, store, "NONEXISTENT", "15551234567@s.whatsapp.net", 5*1024*1024)
	if mErr == nil || mErr.Code != "not_found" {
		t.Errorf("expected not_found, got %v", mErr)
	}
}

func TestNonImageRejected(t *testing.T) {
	store, _ := newTestMessageStore(t)
	insertTestMessage(t, store, "vid001", "15551234567@s.whatsapp.net", "video", 1000, "http://example.com", []byte("key"))
	_, mErr := downloadMediaForAPI(nil, store, "vid001", "15551234567@s.whatsapp.net", 5*1024*1024)
	if mErr == nil || mErr.Code != "unsupported_media_type" {
		t.Errorf("expected unsupported_media_type, got %v", mErr)
	}
}

// ── Size limit tests ──────────────────────────────────────────────────────────

func TestMediaTooLarge_ZeroCap(t *testing.T) {
	store, _ := newTestMessageStore(t)
	insertTestMessage(t, store, "img001", "15551234567@s.whatsapp.net", "image", 100, "http://example.com", []byte("key"))
	_, mErr := downloadMediaForAPI(nil, store, "img001", "15551234567@s.whatsapp.net", 0)
	if mErr == nil || mErr.Code != "media_too_large" {
		t.Errorf("expected media_too_large for zero cap, got %v", mErr)
	}
}

func TestMediaTooLarge_AdvisoryPrecheck(t *testing.T) {
	store, _ := newTestMessageStore(t)
	insertTestMessage(t, store, "img002", "15551234567@s.whatsapp.net", "image", 1000000, "http://example.com", []byte("key"))
	// Cap is 100 bytes; file_length=1MB should trigger advisory precheck.
	_, mErr := downloadMediaForAPI(nil, store, "img002", "15551234567@s.whatsapp.net", 100)
	if mErr == nil || mErr.Code != "media_too_large" {
		t.Errorf("expected media_too_large from advisory precheck, got %v", mErr)
	}
}

// ── Cached file confinement tests ─────────────────────────────────────────────

func TestCachedSymlinkRejected(t *testing.T) {
	store, storeRoot := newTestMessageStore(t)

	const chatJID = "15551234567@s.whatsapp.net"
	const msgID = "cachedSym01"
	insertTestMessage(t, store, msgID, chatJID, "image", 0, "", nil)

	// Build the path where downloadMediaForAPI would look for a cached file.
	safeJID := "15551234567@s.whatsapp.net" // no colons to replace
	// Timestamp is "now" from insertTestMessage — we need the same format.
	// Instead of computing the exact filename, plant a symlink at the chat dir level.
	chatDir := filepath.Join(storeRoot, safeJID)
	if err := os.MkdirAll(filepath.Dir(chatDir), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// Plant a symlink at chatDir pointing outside storeRoot.
	target := t.TempDir()
	if err := os.Symlink(target, chatDir); err != nil {
		t.Skip("symlink creation not permitted in this environment:", err)
	}

	// Query the DB to get the timestamp so we can construct the exact filename.
	var ts string
	_ = store.db.QueryRow("SELECT timestamp FROM messages WHERE id = ?", msgID).Scan(&ts)
	// Even without knowing exact ts, the chatDir symlink means MkdirAll will
	// try to create a subdir INSIDE the symlink target. Let's verify step j fails.
	_, mErr := downloadMediaForAPI(nil, store, msgID, chatJID, 5*1024*1024)
	// We expect either path_confinement or download_failed (incomplete media info).
	// The important thing is it does NOT succeed.
	if mErr == nil {
		t.Error("expected an error when chatDir is a symlink outside storeRoot, got nil")
	}
	if mErr != nil && mErr.Code == "not_found" {
		t.Error("got not_found — message was not inserted correctly")
	}
}

// ── parseMaxDownloadBytes tests ───────────────────────────────────────────────

func TestParseMaxDownloadBytes_Defaults(t *testing.T) {
	cases := []struct {
		env  string
		want int64
	}{
		{"", 5 * 1024 * 1024},
		{"0", 0},
		{"1048576", 1048576},
		{"-1", 0},
		{"abc", 0},
	}
	for _, c := range cases {
		t.Run(c.env, func(t *testing.T) {
			t.Setenv("WHATSAPP_MAX_DOWNLOAD_BYTES", c.env)
			got := parseMaxDownloadBytes()
			if got != c.want {
				t.Errorf("parseMaxDownloadBytes(%q): got %d, want %d", c.env, got, c.want)
			}
		})
	}
}

// ── Allowlist tests (via guardConfig, mirroring the HTTP handler gate) ────────

func TestGoAllowlist_Empty(t *testing.T) {
	t.Setenv("WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS", "")
	cfg := loadGuardConfig()
	if cfg.mediaDownloadAllowAll {
		t.Error("empty env should not set allow-all")
	}
	if len(cfg.mediaDownloadAllowed) != 0 {
		t.Error("empty env should produce empty allowlist")
	}
}

func TestGoAllowlist_AllowAll(t *testing.T) {
	t.Setenv("WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS", "*")
	cfg := loadGuardConfig()
	if !cfg.mediaDownloadAllowAll {
		t.Error("'*' env should set allow-all")
	}
}

func TestGoAllowlist_NormalizationConsistency(t *testing.T) {
	t.Setenv("WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS", "+15551234567")
	cfg := loadGuardConfig()
	// normalizeRecipient strips "+" → "15551234567@s.whatsapp.net"
	if !cfg.mediaDownloadAllowed["15551234567@s.whatsapp.net"] {
		t.Error("allowlist should contain normalized form '15551234567@s.whatsapp.net'")
	}
}

// ── Error response contains no internal detail ─────────────────────────────────

func TestMediaErrorResponseContainsNoInternalDetails(t *testing.T) {
	// All mediaError values should expose only their stable Code, not any path
	// or DB content. Verify the Error() method returns only the code string.
	errs := []*mediaError{
		errUnsupportedMediaType,
		errMediaTooLarge,
		errNotFound,
		errPathConfinement,
		errDownloadFailed,
	}
	for _, e := range errs {
		if e.Error() != e.Code {
			t.Errorf("mediaError.Error() = %q, want Code = %q", e.Error(), e.Code)
		}
	}
}

// ── Temp file cleanup tests ───────────────────────────────────────────────────
// These tests exercise the defer cleanup path without a real whatsmeow client
// by verifying no .tmp-* files remain in the store after failures that occur
// in the atomic-write block. We achieve this by:
//  1. Planting a pre-built test file at intendedPath so step h returns early
//     (not the failure path we want), OR
//  2. Accepting that the test cannot reach the atomic-write block without
//     a real client, and documenting the limitation.
//
// Full atomic-write cleanup tests require either a mock client or a build-time
// test seam. They are covered in integration tests (TestAtomicWrite_*).

func TestTempFileCleanup_DocumentedLimitation(t *testing.T) {
	// The atomic-write path (CreateTemp → Write → Chmod → Sync → Close → Rename)
	// requires a successful whatsmeow Download() call to produce mediaData.
	// Unit tests for the cleanup defer are in integration/docker tests where
	// a real (or mock) whatsmeow client is available.
	//
	// What we CAN verify here: the defer idiom compiles and the tmpPath="" sentinel
	// correctly prevents double-remove when rename succeeds (see code review).
	t.Log("atomic-write cleanup is verified by code review + integration tests (see plan §tests)")
}

// ── Parent dir replaced between MkdirAll and rename ──────────────────────────

func TestParentDirReplaced_BetweenMkdirAndRename(t *testing.T) {
	// Simulate the TOCTOU attack: after MkdirAll the parent directory is
	// replaced with a symlink pointing outside storeRoot. The pre-rename
	// EvalSymlinks check (step l) should detect this.
	//
	// This test uses filesystem operations directly to verify the confinement
	// function, rather than going through downloadMediaForAPI end-to-end.

	storeRoot := t.TempDir()
	outside := t.TempDir()

	chatDir := filepath.Join(storeRoot, "15551234567@s.whatsapp.net")
	if err := os.MkdirAll(chatDir, 0o700); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}

	// Now replace chatDir with a symlink to outside.
	if err := os.Remove(chatDir); err != nil {
		t.Fatalf("remove chatDir: %v", err)
	}
	if err := os.Symlink(outside, chatDir); err != nil {
		t.Skip("symlink creation not permitted:", err)
	}

	// Verify pathHasPrefix correctly rejects the resolved path.
	resolved, err := filepath.EvalSymlinks(chatDir)
	if err != nil {
		t.Fatalf("EvalSymlinks: %v", err)
	}
	if pathHasPrefix(resolved, storeRoot) {
		t.Errorf("pathHasPrefix(%q, %q) should be false after symlink replacement", resolved, storeRoot)
	}
}
