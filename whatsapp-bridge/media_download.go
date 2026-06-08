package main

// Hardened /api/download endpoint — image receive for MCP clients.
//
// Part of the eadam/whatsapp-mcp `homelab` hardening branch.
//
// Security properties:
//   - Input validation: chat_jid and message_id pass a strict character allowlist
//     and an explicit ".." component check before any I/O.
//   - Path confinement: every computed path is checked against messageStore.storeRoot
//     using filepath.Rel (pre-MkdirAll), EvalSymlinks (post-MkdirAll and pre-rename),
//     and a final Lstat+EvalSymlinks (post-rename). Symlinks at the intended target
//     or in the parent chain are rejected.
//   - Size limits: DB advisory check before download, actual-size check after.
//   - Atomic write: CreateTemp + Write + Chmod + Sync + Close + Rename; temp file
//     is always cleaned up by defer, even on write/close/rename failure.
//   - Typed errors: stable codes returned to callers; internal detail logged only.

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
)

// mediaError is a typed, stable error for the /api/download endpoint.
// Code is the machine-readable string returned to the caller in {"code": ...}.
// StatusCode is the HTTP status to respond with.
// Internal detail is NEVER included in the JSON response — log server-side only.
type mediaError struct {
	Code       string
	StatusCode int
}

func (e *mediaError) Error() string { return e.Code }

// Stable codes returned to the API caller. Add new codes here, never inline strings.
var (
	errUnsupportedMediaType = &mediaError{Code: "unsupported_media_type", StatusCode: 400}
	errMediaTooLarge        = &mediaError{Code: "media_too_large", StatusCode: 400}
	errNotFound             = &mediaError{Code: "not_found", StatusCode: 404}
	errPathConfinement      = &mediaError{Code: "path_confinement", StatusCode: 403}
	errDownloadFailed       = &mediaError{Code: "download_failed", StatusCode: 500}
)

// identifierRe is the character allowlist for chat_jid and message_id values.
// Permits: digits, letters (A-Za-z), +, @, ., -, _ .
// Rejects: /, \, NUL, whitespace, and all other characters.
// Note: ".." still passes this regex (two literal dots), so an explicit
// filepath.Clean-based component check follows in downloadMediaForAPI.
var identifierRe = regexp.MustCompile(`^[0-9A-Za-z+@._\-]+$`)

// parseMaxDownloadBytes reads WHATSAPP_MAX_DOWNLOAD_BYTES.
//
//   - Missing/empty → 5 MiB default (5 242 880 bytes).
//   - "0"           → 0 (deny all downloads).
//   - Negative or non-numeric → 0 + log (fail-closed).
func parseMaxDownloadBytes() int64 {
	raw := strings.TrimSpace(os.Getenv("WHATSAPP_MAX_DOWNLOAD_BYTES"))
	if raw == "" {
		return 5 * 1024 * 1024 // 5 MiB
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || v < 0 {
		fmt.Printf("media-download: WHATSAPP_MAX_DOWNLOAD_BYTES=%q is invalid; denying all downloads (fail-closed)\n", raw)
		return 0
	}
	return v
}

// downloadAPIResult is returned by downloadMediaForAPI on success.
type downloadAPIResult struct {
	Path     string // absolute, EvalSymlinks-resolved path inside storeRoot
	Filename string // basename only
}

// downloadMediaForAPI is the hardened download implementation for the /api/download
// HTTP endpoint. It is NOT the same as the internal downloadMedia() function used
// by handleMessage — that function remains for async/sync webhook purposes and
// operates with a looser path model (relative "store/" prefix).
//
// The HTTP handler is responsible for:
//   - Allowlist check (guardCfg.mediaDownloadAllowed / mediaDownloadAllowAll)
//   - Connected check (client.IsConnected())
//
// This function handles all DB, filesystem, and network I/O for the download.
//
// Steps follow the plan exactly (a–n).
func downloadMediaForAPI(
	client *whatsmeow.Client,
	messageStore *MessageStore,
	messageID, chatJID string,
	maxDownloadBytes int64,
) (*downloadAPIResult, *mediaError) {
	sep := string(os.PathSeparator)

	// ── Step a: Input validation (charset + explicit ".." component check) ──
	if !identifierRe.MatchString(messageID) {
		fmt.Printf("media-download: rejected message_id with disallowed chars (len=%d)\n", len(messageID))
		return nil, errPathConfinement
	}
	if !identifierRe.MatchString(chatJID) {
		fmt.Printf("media-download: rejected chat_jid with disallowed chars (len=%d)\n", len(chatJID))
		return nil, errPathConfinement
	}
	// Belt-and-suspenders: reject any identifier whose filepath.Clean form
	// contains ".." as a component, even though "/" is not in the allowed charset.
	// This ensures future charset relaxations cannot introduce traversal.
	for _, id := range []string{messageID, chatJID} {
		cleaned := filepath.Clean(id)
		if cleaned == ".." || strings.HasPrefix(cleaned, ".."+sep) ||
			strings.Contains(cleaned, sep+".."+sep) ||
			strings.HasSuffix(cleaned, sep+"..") {
			fmt.Printf("media-download: rejected identifier containing .. component\n")
			return nil, errPathConfinement
		}
	}

	// ── Step b: Query DB for media metadata ────────────────────────────────
	var mediaType, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var msgTimestamp time.Time

	err := messageStore.db.QueryRow(
		"SELECT media_type, url, media_key, file_sha256, file_enc_sha256, file_length, timestamp"+
			" FROM messages WHERE id = ? AND chat_jid = ?",
		messageID, chatJID,
	).Scan(&mediaType, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength, &msgTimestamp)
	if err == sql.ErrNoRows {
		return nil, errNotFound
	}
	if err != nil {
		fmt.Printf("media-download: DB query failed (internal detail omitted)\n")
		return nil, errNotFound
	}

	// ── Step c: Reject non-image ────────────────────────────────────────────
	if mediaType != "image" {
		// Log media_type for ops, but never echo it to the caller.
		fmt.Printf("media-download: rejected non-image media_type=%q for message=%s\n", mediaType, messageID)
		return nil, errUnsupportedMediaType
	}

	// ── Step d: Deny-all when configured max is zero ────────────────────────
	if maxDownloadBytes == 0 {
		return nil, errMediaTooLarge
	}

	// ── Step e: Advisory size precheck from DB ──────────────────────────────
	// file_length is a hint from WhatsApp — not authoritative (actual byte count
	// checked in step k after download). Zero means unknown; skip the precheck.
	if fileLength > 0 && int64(fileLength) > maxDownloadBytes {
		fmt.Printf("media-download: advisory precheck: file_length=%d > max=%d\n", fileLength, maxDownloadBytes)
		return nil, errMediaTooLarge
	}

	// ── Step f: Compute intended path ───────────────────────────────────────
	// Match the existing downloadMedia() sanitization convention (: → _) so
	// that files already cached by the webhook sync path are found at step h.
	safeJID := strings.ReplaceAll(chatJID, ":", "_")
	filename := fmt.Sprintf("image_%s_%s.jpg", msgTimestamp.Format("20060102_150405"), messageID)
	intendedPath := filepath.Join(messageStore.storeRoot, safeJID, filename)

	// ── Step g: filepath.Rel confinement check ──────────────────────────────
	rel, relErr := filepath.Rel(messageStore.storeRoot, filepath.Clean(intendedPath))
	if relErr != nil || rel == ".." || strings.HasPrefix(rel, ".."+sep) {
		fmt.Printf("media-download: path confinement failed at Rel check (internal)\n")
		return nil, errPathConfinement
	}

	// ── Step h: Cached file check — must not fall through to download ───────
	if lstat, lstatErr := os.Lstat(intendedPath); lstatErr == nil {
		// File exists. Reject symlinks — never follow them.
		if lstat.Mode()&os.ModeSymlink != 0 {
			fmt.Printf("media-download: cached file is a symlink, rejecting\n")
			return nil, errPathConfinement
		}
		// Use actual stat().Size() — same as lstat().Size() for regular files.
		if lstat.Size() > maxDownloadBytes {
			return nil, errMediaTooLarge
		}
		resolved, resolveErr := filepath.EvalSymlinks(intendedPath)
		if resolveErr != nil {
			fmt.Printf("media-download: EvalSymlinks on cached file failed (internal)\n")
			return nil, errDownloadFailed
		}
		if !pathHasPrefix(resolved, messageStore.storeRoot) {
			fmt.Printf("media-download: cached file resolved outside storeRoot (internal)\n")
			return nil, errPathConfinement
		}
		return &downloadAPIResult{Path: resolved, Filename: filename}, nil
		// Do NOT fall through to the download steps.
	}

	// ── Step i: MkdirAll for the JID-named directory ────────────────────────
	parentDir := filepath.Dir(intendedPath)
	if mkErr := os.MkdirAll(parentDir, 0o700); mkErr != nil {
		fmt.Printf("media-download: MkdirAll failed (internal)\n")
		return nil, errDownloadFailed
	}

	// ── Step j: Post-MkdirAll parent confinement with EvalSymlinks ──────────
	realParent, evalErr := filepath.EvalSymlinks(parentDir)
	if evalErr != nil {
		fmt.Printf("media-download: EvalSymlinks on parent failed (internal)\n")
		return nil, errDownloadFailed
	}
	if !pathHasPrefix(realParent, messageStore.storeRoot) {
		fmt.Printf("media-download: parent dir is outside storeRoot after MkdirAll\n")
		return nil, errPathConfinement
	}

	// ── Step k: Download via whatsmeow ──────────────────────────────────────
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 {
		fmt.Printf("media-download: incomplete media info for message=%s, cannot download\n", messageID)
		return nil, errDownloadFailed
	}
	directPath := extractDirectPathFromURL(url)
	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     whatsmeow.MediaImage,
	}
	mediaData, dlErr := client.Download(context.Background(), downloader)
	if dlErr != nil {
		fmt.Printf("media-download: Download() failed (internal detail omitted)\n")
		return nil, errDownloadFailed
	}

	// Actual downloaded size check — authoritative; overrides the DB hint.
	if int64(len(mediaData)) > maxDownloadBytes {
		fmt.Printf("media-download: actual downloaded size %d > max %d\n", len(mediaData), maxDownloadBytes)
		return nil, errMediaTooLarge
	}

	// ── Step l: Atomic write — temp file always cleaned up by defer ─────────
	tmpFile, createErr := os.CreateTemp(parentDir, ".tmp-")
	if createErr != nil {
		fmt.Printf("media-download: CreateTemp failed (internal)\n")
		return nil, errDownloadFailed
	}
	tmpPath := tmpFile.Name()
	// Deferred cleanup: removes the temp file if tmpPath is still non-empty.
	// Set tmpPath = "" after a successful rename to skip the removal.
	defer func() {
		if tmpPath != "" {
			_ = os.Remove(tmpPath)
		}
	}()

	n, writeErr := tmpFile.Write(mediaData)
	if writeErr != nil || n != len(mediaData) {
		fmt.Printf("media-download: write to temp file failed (internal)\n")
		_ = tmpFile.Close()
		return nil, errDownloadFailed
	}
	if chmodErr := tmpFile.Chmod(0o600); chmodErr != nil {
		fmt.Printf("media-download: chmod 0600 failed (internal)\n")
		_ = tmpFile.Close()
		return nil, errDownloadFailed
	}
	if syncErr := tmpFile.Sync(); syncErr != nil {
		fmt.Printf("media-download: sync failed (internal)\n")
		_ = tmpFile.Close()
		return nil, errDownloadFailed
	}
	if closeErr := tmpFile.Close(); closeErr != nil {
		fmt.Printf("media-download: close failed (internal)\n")
		return nil, errDownloadFailed
	}

	// Re-validate parent immediately before rename: an attacker or buggy process
	// could have replaced the directory with a symlink between MkdirAll and here.
	realParent2, evalErr2 := filepath.EvalSymlinks(parentDir)
	if evalErr2 != nil {
		fmt.Printf("media-download: pre-rename EvalSymlinks on parent failed (internal)\n")
		return nil, errDownloadFailed
	}
	if !pathHasPrefix(realParent2, messageStore.storeRoot) {
		fmt.Printf("media-download: parent dir escaped storeRoot before rename\n")
		return nil, errPathConfinement
	}

	if renameErr := os.Rename(tmpPath, intendedPath); renameErr != nil {
		fmt.Printf("media-download: rename failed (internal)\n")
		return nil, errDownloadFailed
	}
	tmpPath = "" // Prevent defer from removing the successfully renamed file.

	// ── Step m: Final confinement check (post-rename) ───────────────────────
	finalStat, lstatFinalErr := os.Lstat(intendedPath)
	if lstatFinalErr != nil {
		fmt.Printf("media-download: Lstat on final path failed (internal)\n")
		return nil, errDownloadFailed
	}
	if finalStat.Mode()&os.ModeSymlink != 0 {
		// A race: something replaced the file we just renamed with a symlink.
		fmt.Printf("media-download: final path is a symlink after rename (race detected)\n")
		_ = os.Remove(intendedPath)
		return nil, errPathConfinement
	}
	resolved, finalEvalErr := filepath.EvalSymlinks(intendedPath)
	if finalEvalErr != nil {
		fmt.Printf("media-download: final EvalSymlinks failed (internal)\n")
		return nil, errDownloadFailed
	}
	if !pathHasPrefix(resolved, messageStore.storeRoot) {
		fmt.Printf("media-download: final path escaped storeRoot after all checks\n")
		return nil, errPathConfinement
	}

	// ── Step n: Return ──────────────────────────────────────────────────────
	fmt.Printf("media-download: ✅ message=%s size=%d -> %s\n", messageID, len(mediaData), resolved)
	return &downloadAPIResult{Path: resolved, Filename: filename}, nil
}
