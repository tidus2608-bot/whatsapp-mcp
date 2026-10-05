package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRequireAPIAuth(t *testing.T) {
	handler := requireAPIAuth("secret", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	tests := []struct {
		name          string
		authorization string
		contentType   string
		want          int
	}{
		{"no token", "", "application/json", http.StatusUnauthorized},
		{"wrong token", "Bearer nope", "application/json", http.StatusUnauthorized},
		{"token without scheme", "secret", "application/json", http.StatusUnauthorized},
		{"text/plain cross-site style request", "Bearer secret", "text/plain", http.StatusUnsupportedMediaType},
		{"form request", "Bearer secret", "application/x-www-form-urlencoded", http.StatusUnsupportedMediaType},
		{"missing content type", "Bearer secret", "", http.StatusUnsupportedMediaType},
		{"valid", "Bearer secret", "application/json", http.StatusOK},
		{"valid with charset", "Bearer secret", "application/json; charset=utf-8", http.StatusOK},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/api/send", strings.NewReader(`{}`))
			if tt.authorization != "" {
				req.Header.Set("Authorization", tt.authorization)
			}
			if tt.contentType != "" {
				req.Header.Set("Content-Type", tt.contentType)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != tt.want {
				t.Errorf("got status %d, want %d", rec.Code, tt.want)
			}
		})
	}
}

func TestLoadOrCreateAPIToken(t *testing.T) {
	t.Setenv("WHATSAPP_API_TOKEN", "")
	path := filepath.Join(t.TempDir(), "store", apiTokenFile)

	token, err := loadOrCreateAPIToken(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(token) != 64 {
		t.Errorf("expected a 64-character hex token, got %q", token)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("token file permissions = %o, want 600", perm)
	}

	again, err := loadOrCreateAPIToken(path)
	if err != nil {
		t.Fatal(err)
	}
	if again != token {
		t.Error("token changed between runs; the MCP server would lose access after a bridge restart")
	}

	t.Setenv("WHATSAPP_API_TOKEN", "from-env")
	fromEnv, err := loadOrCreateAPIToken(path)
	if err != nil {
		t.Fatal(err)
	}
	if fromEnv != "from-env" {
		t.Errorf("WHATSAPP_API_TOKEN should take precedence, got %q", fromEnv)
	}
}

func TestResolveMediaPath(t *testing.T) {
	root := t.TempDir()
	outbox := filepath.Join(root, "outbox")
	store := filepath.Join(root, "store")
	elsewhere := filepath.Join(root, "elsewhere")
	for _, dir := range []string{outbox, store, elsewhere} {
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}

	write := func(path string) string {
		if err := os.WriteFile(path, []byte("data"), 0600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	allowedFile := write(filepath.Join(outbox, "photo.jpg"))
	secretFile := write(filepath.Join(elsewhere, "id_rsa"))
	sessionDB := write(filepath.Join(store, "whatsapp.db"))

	link := filepath.Join(outbox, "innocent.jpg")
	if err := os.Symlink(secretFile, link); err != nil {
		t.Fatal(err)
	}

	allowed := []string{outbox}

	if got, err := resolveMediaPath(allowedFile, allowed, store); err != nil {
		t.Errorf("file in outbox rejected: %v", err)
	} else if want, _ := filepath.EvalSymlinks(allowedFile); got != want {
		t.Errorf("got %q, want %q", got, want)
	}

	rejected := map[string]string{
		"file outside allowed folders": secretFile,
		"traversal out of the outbox":  filepath.Join(outbox, "..", "elsewhere", "id_rsa"),
		"symlink pointing outside":     link,
		"directory":                    outbox,
		"missing file":                 filepath.Join(outbox, "missing.jpg"),
	}
	for name, path := range rejected {
		if _, err := resolveMediaPath(path, allowed, store); err == nil {
			t.Errorf("%s: %s was allowed", name, path)
		}
	}

	// The store holds the session keys: refuse it even when a wider folder is allowed
	if _, err := resolveMediaPath(sessionDB, []string{root}, store); err == nil {
		t.Error("session database in the store was allowed")
	}

	// Error message tells the caller where files are accepted
	_, err := resolveMediaPath(secretFile, allowed, store)
	if err == nil || !strings.Contains(err.Error(), outbox) {
		t.Errorf("error should name the allowed folder, got %v", err)
	}
}

func TestMediaDirsFromEnv(t *testing.T) {
	t.Setenv("WHATSAPP_MEDIA_DIRS", "")
	if dirs := mediaDirsFromEnv(); len(dirs) != 1 || dirs[0] != outboxDir {
		t.Errorf("default should be the outbox only, got %v", dirs)
	}

	t.Setenv("WHATSAPP_MEDIA_DIRS", strings.Join([]string{"/a", " ", "/b"}, string(os.PathListSeparator)))
	dirs := mediaDirsFromEnv()
	if len(dirs) != 3 || dirs[0] != outboxDir || dirs[1] != "/a" || dirs[2] != "/b" {
		t.Errorf("got %v, want outbox plus /a and /b", dirs)
	}
}

func TestSafeFileName(t *testing.T) {
	tests := map[string]string{
		"report.pdf":               "report.pdf",
		"../../../.bashrc":         "_.._.._.bashrc",
		`..\..\Windows\evil.exe`:   "_.._Windows_evil.exe",
		"..":                       "file",
		"":                         "file",
		".hidden":                  "hidden",
		"Báo cáo tháng 9.docx":     "Báo_cáo_tháng_9.docx",
		"123456789@s.whatsapp.net": "123456789@s.whatsapp.net",
		"123:4@s.whatsapp.net":     "123_4@s.whatsapp.net",
	}
	for in, want := range tests {
		if got := safeFileName(in); got != want {
			t.Errorf("safeFileName(%q) = %q, want %q", in, got, want)
		}
	}

	long := safeFileName(strings.Repeat("é", 200) + ".pdf")
	if len(long) > 120 || !strings.HasSuffix(long, ".pdf") {
		t.Errorf("long name not shortened with extension kept: %d bytes, %q", len(long), long[len(long)-8:])
	}

	for _, name := range []string{"../../x", `a\b`, "a/b", "..", "."} {
		got := safeFileName(name)
		if strings.ContainsAny(got, `/\`) || got == "." || got == ".." {
			t.Errorf("safeFileName(%q) = %q is not a single safe path element", name, got)
		}
	}
}

func TestMediaLocalFileNameIsUniquePerMessage(t *testing.T) {
	// Two images received in the same second get the same stored filename
	a := mediaLocalFileName("3EB0AAA", "image_20250401_120000.jpg")
	b := mediaLocalFileName("3EB0BBB", "image_20250401_120000.jpg")
	if a == b {
		t.Errorf("different messages map to the same file %q", a)
	}
}

func TestParticipantUser(t *testing.T) {
	tests := map[string]string{
		"447700900123@s.whatsapp.net":    "447700900123",
		"447700900123:12@s.whatsapp.net": "447700900123",
		"447700900123":                   "447700900123",
	}
	for in, want := range tests {
		if got := participantUser(in); got != want {
			t.Errorf("participantUser(%q) = %q, want %q", in, got, want)
		}
	}
}
