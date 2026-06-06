package main

// Homelab admin: campaign manifest loader.
//
// Part of the eadam/whatsapp-mcp `homelab` hardening branch (not upstream).
// Invoked as a one-off process — `whatsapp-bridge -load-manifest <file>
// -manifest-campaign <id>` — typically via `docker compose exec`. It writes the
// operator-approved (recipient, message_hash) pairs into the bridge-owned
// send_manifest table. The MCP server has no path to this; the manifest is
// immutable to the agent. At send time, with WHATSAPP_CAMPAIGN_ID set to the
// same id, the bridge will deliver ONLY messages whose (recipient, hash) is in
// this manifest (see enforce.go reserveSend).
//
// Manifest file format — a JSON array of objects:
//   [
//     {"recipient": "+15551234567", "message": "Hi Ada — loved your visit to Japan! ..."},
//     {"recipient": "+15557654321", "message": "Hi Ben — your trip to Peru ..."}
//   ]
//
// The `message` MUST be byte-identical to what will later be sent (the hash must
// match), so load the exact approved text Claude will send.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"time"
)

type manifestEntry struct {
	Recipient string `json:"recipient"`
	Message   string `json:"message"`
}

func runManifestAdmin(path, campaignID string) error {
	if campaignID == "" {
		return fmt.Errorf("-manifest-campaign is required (must match WHATSAPP_CAMPAIGN_ID at send time)")
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("reading manifest file: %v", err)
	}
	var entries []manifestEntry
	if err := json.Unmarshal(raw, &entries); err != nil {
		return fmt.Errorf("parsing manifest JSON (expected an array of {recipient,message}): %v", err)
	}
	if len(entries) == 0 {
		return fmt.Errorf("manifest is empty")
	}

	if err := os.MkdirAll("store", 0755); err != nil {
		return fmt.Errorf("creating store dir: %v", err)
	}
	// Short-lived writer against the same DB the running bridge uses; a busy
	// timeout absorbs brief contention with the live bridge.
	db, err := sql.Open("sqlite3", "file:store/messages.db?_foreign_keys=on&_busy_timeout=5000")
	if err != nil {
		return fmt.Errorf("opening message DB: %v", err)
	}
	defer func() { _ = db.Close() }()

	if err := ensureGuardSchema(db); err != nil {
		return err
	}

	now := time.Now().Unix()
	inserted, skipped, rejected := 0, 0, 0
	for i, e := range entries {
		canon, nErr := normalizeRecipient(e.Recipient)
		if nErr != nil {
			fmt.Fprintf(os.Stderr, "  entry %d: rejected recipient %q: %v\n", i, e.Recipient, nErr)
			rejected++
			continue
		}
		if e.Message == "" {
			fmt.Fprintf(os.Stderr, "  entry %d: rejected empty message for %s\n", i, canon)
			rejected++
			continue
		}
		h := messageHash(e.Message, "")
		res, err := db.Exec(
			`INSERT OR IGNORE INTO send_manifest(campaign_id, recipient, message_hash, created_at) VALUES (?,?,?,?)`,
			campaignID, canon, h, now)
		if err != nil {
			return fmt.Errorf("inserting manifest row for %s: %v", canon, err)
		}
		if n, _ := res.RowsAffected(); n > 0 {
			inserted++
		} else {
			skipped++
		}
	}

	_, _ = db.Exec(`INSERT INTO send_audit(ts, campaign_id, decision, detail) VALUES (?,?,?,?)`,
		now, campaignID, "manifest-loaded",
		fmt.Sprintf("inserted=%d skipped=%d rejected=%d from %s", inserted, skipped, rejected, path))

	fmt.Printf("manifest %q loaded for campaign %q: inserted=%d skipped(dupe)=%d rejected=%d\n",
		path, campaignID, inserted, skipped, rejected)
	if rejected > 0 {
		return fmt.Errorf("%d entry(ies) rejected — fix the manifest and re-run", rejected)
	}
	return nil
}
