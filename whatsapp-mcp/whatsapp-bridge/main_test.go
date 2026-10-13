package main

import (
	"database/sql"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	waLog "go.mau.fi/whatsmeow/util/log"
)

func TestShouldTriggerIncoming(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "/tmp/evento-wsp")
	t.Setenv("WSP_JID", "objetivo@lid")

	tests := []struct {
		name     string
		chatJID  string
		isNew    bool
		expected bool
	}{
		{"de ella, nuevo", "objetivo@lid", true, true},
		{"otro chat", "otro@lid", true, false},
		// Lo de Mikel desde su telefono TIENE que despertar: es como le habla a la tulpa
		// y como le manda /on, /off, /sticker y /sys.
		{"propio nuevo", "objetivo@lid", true, true},
		{"repetido", "objetivo@lid", false, false},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := shouldTriggerIncoming(test.chatJID, "MSGID", test.isNew)
			if got != test.expected {
				t.Fatalf("shouldTriggerIncoming() = %v, want %v", got, test.expected)
			}
		})
	}
}

// Lo unico que separa a este bot de contestarse a si mismo, ahora que lo propio despierta.
func TestShouldTriggerIgnoraElEcoPropio(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "/tmp/evento-wsp")
	t.Setenv("WSP_JID", "objetivo@lid")

	rememberSentByAPI("DELATULPA")
	if shouldTriggerIncoming("objetivo@lid", "DELATULPA", true) {
		t.Fatal("un mensaje mandado por /api/send no puede despertar al modelo")
	}
	// El id se consume al mirarlo, asi que la entrada no se queda ahi para siempre.
	if isOurOwnEcho("DELATULPA") {
		t.Fatal("el id tenia que borrarse al consumirlo")
	}
	if !shouldTriggerIncoming("objetivo@lid", "DEMIKEL", true) {
		t.Fatal("un id que no es nuestro tiene que despertar igual")
	}
}

func TestSentByAPICacheExpiresAndStaysBounded(t *testing.T) {
	now := time.Unix(100, 0)
	cache := newSentMessageCache(time.Minute, 2)
	cache.remember("uno", now)
	cache.remember("dos", now.Add(time.Second))
	cache.remember("tres", now.Add(2*time.Second))
	if got := cache.size(now.Add(2 * time.Second)); got != 2 {
		t.Fatalf("cache size = %d, want 2", got)
	}
	if cache.consume("uno", now.Add(2*time.Second)) {
		t.Fatal("la entrada mas vieja tenia que salir al alcanzar el limite")
	}
	if got := cache.size(now.Add(2 * time.Minute)); got != 0 {
		t.Fatalf("expired cache size = %d, want 0", got)
	}
}

func TestMudoRejectsBeforeWhatsApp(t *testing.T) {
	path := filepath.Join(t.TempDir(), "MUDO")
	t.Setenv("WSP_MUDO", path)

	open := httptest.NewRecorder()
	if rejectIfMudo(open) {
		t.Fatal("un MUDO inexistente no debe bloquear")
	}
	if err := os.WriteFile(path, []byte(""), 0o600); err != nil {
		t.Fatal(err)
	}
	locked := httptest.NewRecorder()
	if !rejectIfMudo(locked) {
		t.Fatal("MUDO tenia que bloquear")
	}
	if locked.Code != http.StatusLocked {
		t.Fatalf("status = %d, want %d", locked.Code, http.StatusLocked)
	}
}

func TestMessageStoreWaitsForShortSQLiteLocks(t *testing.T) {
	old, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(old)

	store, err := NewMessageStore()
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	var timeout int
	if err := store.db.QueryRow("PRAGMA busy_timeout").Scan(&timeout); err != nil {
		t.Fatal(err)
	}
	if timeout != 5000 {
		t.Fatalf("busy_timeout = %d, want 5000", timeout)
	}

	locker, err := sql.Open("sqlite3", "file:store/messages.db?_busy_timeout=5000")
	if err != nil {
		t.Fatal(err)
	}
	defer locker.Close()
	tx, err := locker.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(
		"INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		"locked@lid", "Locked", time.Now(),
	); err != nil {
		t.Fatal(err)
	}
	released := make(chan error, 1)
	go func() {
		time.Sleep(100 * time.Millisecond)
		released <- tx.Commit()
	}()
	if err := store.StoreChat("after@lid", "After", time.Now()); err != nil {
		t.Fatalf("la escritura no espero el bloqueo breve: %v", err)
	}
	if err := <-released; err != nil {
		t.Fatalf("no se pudo liberar el bloqueo: %v", err)
	}
}

func TestShouldTriggerCanBeDisabled(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "")
	t.Setenv("WSP_JID", "objetivo@lid")
	if shouldTriggerIncoming("objetivo@lid", "MSGID", true) {
		t.Fatal("empty WSP_EVENT_TRIGGER must disable event wakes")
	}
}

// El watcher es lo unico que queda vigilando la salud del bridge, asi que no puede ser
// hijo suyo: un bridge que muere se lleva a sus hijos y apagaria el aviso justo cuando
// hace falta. Se comprueba sobre el proceso real y no sobre la config.
func TestEventWatcherSurvivesTheBridge(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "sid")
	script := filepath.Join(dir, "watcher.sh")
	// Con Setsid el hijo es lider de su propia sesion, o sea que su SID es su propio PID.
	body := "#!/bin/sh\nprintf '%s %s' \"$$\" \"$(ps -o sid= -p $$)\" > " + marker + "\nsleep 5\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("no se pudo escribir el watcher falso: %v", err)
	}
	t.Setenv("WSP_EVENT_TRIGGER", script)

	startEventWatcher(waLog.Noop)

	var raw []byte
	for i := 0; i < 100; i++ {
		if data, err := os.ReadFile(marker); err == nil && len(strings.Fields(string(data))) == 2 {
			raw = data
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if raw == nil {
		t.Fatal("el watcher no llego a arrancar")
	}
	fields := strings.Fields(string(raw))
	if len(fields) != 2 {
		t.Fatalf("marca ilegible %q", raw)
	}
	pid, err := strconv.Atoi(fields[0])
	if err != nil {
		t.Fatalf("pid ilegible %q: %v", fields[0], err)
	}
	sid, err := strconv.Atoi(fields[1])
	if err != nil {
		t.Fatalf("sid ilegible %q: %v", fields[1], err)
	}
	if sid != pid {
		t.Fatalf("el watcher no es lider de sesion (pid %d, sid %d): moriria con el bridge", pid, sid)
	}
}

func TestEventWatcherDoesNotStartWhenDisabled(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "")
	// No debe reventar ni dejar nada corriendo: es la valvula de escape documentada
	startEventWatcher(waLog.Noop)
}
