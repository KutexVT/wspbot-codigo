package main

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"

	"bytes"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages
	db, err := sql.Open("sqlite3", "file:store/messages.db?_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages 
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length) 
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
	)
	return err
}

// HasMessage distinguishes a live message from a replay of an event WhatsApp already
// delivered. StoreMessage intentionally keeps INSERT OR REPLACE because history sync can
// complete metadata later; only the Codex trigger needs the stricter "new once" signal.
func (store *MessageStore) HasMessage(id, chatJID string) (bool, error) {
	var found int
	err := store.db.QueryRow(
		"SELECT 1 FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1",
		id, chatJID,
	).Scan(&found)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

const defaultEventTrigger = "/home/kutex/WSP Bot/bin/evento-wsp.py"
const defaultTargetChat = "000000000000000@lid"

func eventTriggerPath() string {
	if path, ok := os.LookupEnv("WSP_EVENT_TRIGGER"); ok {
		return path
	}
	return defaultEventTrigger
}

func targetEventChat() string {
	if jid := os.Getenv("WSP_JID"); jid != "" {
		return jid
	}
	return defaultTargetChat
}

const (
	sentByAPITTL = 10 * time.Minute
	sentByAPIMax = 4096
)

type sentMessageCache struct {
	mu      sync.Mutex
	entries map[string]time.Time
}

var sentByAPI = sentMessageCache{entries: make(map[string]time.Time)}

func rememberSentByAPI(messageID string) {
	if messageID == "" {
		return
	}
	now := time.Now()
	sentByAPI.mu.Lock()
	defer sentByAPI.mu.Unlock()
	for id, storedAt := range sentByAPI.entries {
		if now.Sub(storedAt) >= sentByAPITTL {
			delete(sentByAPI.entries, id)
		}
	}
	for len(sentByAPI.entries) >= sentByAPIMax {
		var oldestID string
		var oldestTime time.Time
		for id, storedAt := range sentByAPI.entries {
			if oldestID == "" || storedAt.Before(oldestTime) {
				oldestID, oldestTime = id, storedAt
			}
		}
		delete(sentByAPI.entries, oldestID)
	}
	sentByAPI.entries[messageID] = now
}

func isOurOwnEcho(messageID string) bool {
	sentByAPI.mu.Lock()
	defer sentByAPI.mu.Unlock()
	storedAt, ours := sentByAPI.entries[messageID]
	delete(sentByAPI.entries, messageID)
	return ours && time.Since(storedAt) < sentByAPITTL
}

func shouldTriggerIncoming(chatJID, messageID string, isNew bool) bool {
	return eventTriggerPath() != "" && chatJID == targetEventChat() && isNew &&
		!isOurOwnEcho(messageID)
}

// notifyIncomingMessage never blocks whatsmeow's event handler. The coordinator applies
// the debounce, the cross-process lock and the pending-turn rule. No message body goes on
// the command line, so private chat text is not exposed through ps.
func notifyIncomingMessage(chatJID, messageID string, isFromMe, isNew, direct bool, logger waLog.Logger) {
	if !shouldTriggerIncoming(chatJID, messageID, isNew) {
		return
	}
	path := eventTriggerPath()
	args := []string{"notify", "--chat-jid", chatJID, "--message-id", messageID}
	if isFromMe {
		args = append(args, "--from-me")
	}
	if direct {
		args = append(args, "--direct")
	}
	go func() {
		cmd := exec.Command(path, args...)
		if output, err := cmd.CombinedOutput(); err != nil {
			logger.Warnf("Codex event trigger failed: %v (%s)", err, strings.TrimSpace(string(output)))
		}
	}()
}

func quotedMessageID(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}
	var context *waProto.ContextInfo
	switch {
	case msg.GetExtendedTextMessage() != nil:
		context = msg.GetExtendedTextMessage().GetContextInfo()
	case msg.GetImageMessage() != nil:
		context = msg.GetImageMessage().GetContextInfo()
	case msg.GetVideoMessage() != nil:
		context = msg.GetVideoMessage().GetContextInfo()
	case msg.GetAudioMessage() != nil:
		context = msg.GetAudioMessage().GetContextInfo()
	case msg.GetDocumentMessage() != nil:
		context = msg.GetDocumentMessage().GetContextInfo()
	case msg.GetStickerMessage() != nil:
		context = msg.GetStickerMessage().GetContextInfo()
	}
	return context.GetStanzaID()
}

func repliesToTulpa(store *MessageStore, chatJID string, msg *waProto.Message) bool {
	quotedID := quotedMessageID(msg)
	if quotedID == "" {
		return false
	}
	quoted, err := store.GetQuotedInfo(quotedID, chatJID)
	return err == nil && quoted.Sender == "__tulpa__"
}

// startEventWatcher adds the events that never arrive as a message: a timed MUDO
// expiring, one consideration at the silence threshold, the hourly thread compaction and
// the bridge-down desktop warning. It polls only local state and SQLite; it never calls
// the model on empty minute-by-minute checks.
//
// The watcher is deliberately NOT a child of this process. It is the only thing left
// watching the bridge's own health, and a bridge that dies takes its children with it —
// which would silence the warning exactly when it matters. Setsid detaches it; its own
// watch.lock keeps a restart from starting a second one.
func startEventWatcher(logger waLog.Logger) {
	path := eventTriggerPath()
	if path == "" {
		logger.Infof("Codex event trigger disabled (WSP_EVENT_TRIGGER is empty)")
		return
	}
	cmd := exec.Command(path, "watch")
	cmd.Env = os.Environ()
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		logger.Warnf("Codex event watcher failed to start: %v", err)
		return
	}
	// Release() and not Wait(): the watcher outlives this process on purpose, so there is
	// nobody to reap it here. init adopts it once the session leader detaches.
	if err := cmd.Process.Release(); err != nil {
		logger.Warnf("Codex event watcher could not be released: %v", err)
	}
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}

	// For now, we're ignoring non-text messages
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	MessageID string `json:"message_id"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient string `json:"recipient"`
	Message   string `json:"message"`
	MediaPath string `json:"media_path,omitempty"`
	// QuotedID is the WhatsApp message ID being replied to (the messages.id column).
	// Empty means a plain message, which keeps the exact wire format it always had.
	QuotedID string `json:"quoted_id,omitempty"`
}

// MarkReadRequest is the body of /api/markread. The IDs are picked by the caller
// (pulso.sh) with SQL: the scripts own the queries in this project, and that keeps the
// endpoint from having to know anything about cursors.
type MarkReadRequest struct {
	ChatJID    string   `json:"chat_jid"`
	MessageIDs []string `json:"message_ids"`
}

type MarkReadResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
	Marked  int    `json:"marked"`
}

// Maps a file extension to the same media_type vocabulary extractMediaInfo stores,
// so outgoing media reads back exactly like incoming media does.
func mediaTypeFromPath(path string) string {
	switch strings.ToLower(strings.TrimPrefix(filepath.Ext(path), ".")) {
	case "jpg", "jpeg", "png", "gif":
		return "image"
	// Any .webp goes out as a sticker (see sendWhatsAppMessage), so it has to read
	// back as one too or the local history disagrees with what was actually sent.
	case "webp":
		return "sticker"
	case "mp4", "avi", "mov":
		return "video"
	case "ogg", "mp3", "m4a", "wav":
		return "audio"
	default:
		return "document"
	}
}

// Process start time and last received event, both exposed through /api/health.
var startedAt = time.Now()
var lastEventUnix atomic.Int64

// HealthResponse is what /api/health answers. The bot polls this every minute to tell
// "nothing is happening" apart from "nothing is reaching me" — two states that look
// identical from the database alone, and used to be logged as the former.
type HealthResponse struct {
	Connected     bool  `json:"connected"`
	LoggedIn      bool  `json:"logged_in"`
	UptimeS       int64 `json:"uptime_s"`
	LastEventAgoS int64 `json:"last_event_ago_s"`
	// ReadReceipts is the account-wide privacy setting, "all" or "none". It is here
	// because MarkRead silently degrades to read-self when it is "none" (see
	// receipt.go): the blue ticks would never appear and no error would say why.
	ReadReceipts string `json:"read_receipts"`
}

// acquireLock takes an exclusive, non-blocking lock so only one bridge can ever run.
//
// WhatsApp invalidates the session when two clients connect with the same credentials
// ("Got replaced stream error"): the second one gets in, the first one drops, and
// sometimes the QR has to be scanned again. There is no supervisor process here — the
// bridge is started by hand from a shell alias — so this lock is the only thing standing
// between a second `wspbot` and a dead session.
//
// The returned file must stay open for the whole run: closing it releases the lock.
func acquireLock() (*os.File, error) {
	f, err := os.OpenFile("store/bridge.lock", os.O_CREATE|os.O_RDWR, 0644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("another whatsapp-client already holds store/bridge.lock")
	}
	return f, nil
}

// buildQuoteContext assembles the ContextInfo that turns a message into a reply.
//
// The non-obvious part is QuotedMessage. The receiving phone draws the quoted bubble
// from the quotedMessage carried in this payload, NOT from its own local copy looked up
// by stanzaID — that is precisely why WhatsApp reply-spoofing works at all. So sending
// StanzaID + Participant with a nil QuotedMessage gives an empty or broken quote on her
// side, and we have to rebuild the original from what the database kept.
//
// The rebuild is faithful for text (the 95% case). For media the thumbnail is lost, so
// a quoted photo shows a generic box instead of a preview; everything else lines up.
func buildQuoteContext(client *whatsmeow.Client, messageStore *MessageStore,
	chat types.JID, quotedID string) (*waProto.ContextInfo, error) {

	row, err := messageStore.GetQuotedInfo(quotedID, chat.String())
	if err == sql.ErrNoRows {
		return nil, fmt.Errorf("message %s is not in the local database for chat %s", quotedID, chat)
	} else if err != nil {
		return nil, fmt.Errorf("could not read quoted message %s: %v", quotedID, err)
	}

	// Who sent the message being quoted.
	//
	// For incoming messages the sender column holds the user part with no server, so the
	// chat's own server is the right one to glue on.
	//
	// For our own messages the column is useless: three different values live in there
	// (the phone number, the LID, and the literal "__tulpa__" for anything sent through
	// this API), so rebuilding a JID from it would invent one. The store is the only
	// authority for who we are.
	var participant types.JID
	if row.IsFromMe {
		if chat.Server == types.HiddenUserServer {
			participant = client.Store.GetLID().ToNonAD()
		} else {
			participant = client.Store.GetJID().ToNonAD()
		}
	} else {
		participant = types.JID{User: row.Sender, Server: chat.Server}
	}
	if participant.IsEmpty() {
		return nil, fmt.Errorf("could not work out the sender of %s", quotedID)
	}

	quoted := &waProto.Message{}
	directPath := extractDirectPathFromURL(row.URL)
	switch row.MediaType {
	case "":
		quoted.Conversation = proto.String(row.Content)
	case "image":
		quoted.ImageMessage = &waProto.ImageMessage{
			Caption:       proto.String(row.Content),
			Mimetype:      proto.String("image/jpeg"),
			URL:           proto.String(row.URL),
			DirectPath:    proto.String(directPath),
			MediaKey:      row.MediaKey,
			FileSHA256:    row.FileSHA256,
			FileEncSHA256: row.FileEncSHA256,
			FileLength:    proto.Uint64(row.FileLength),
		}
	case "audio":
		quoted.AudioMessage = &waProto.AudioMessage{
			Mimetype:      proto.String("audio/ogg; codecs=opus"),
			PTT:           proto.Bool(true),
			URL:           proto.String(row.URL),
			DirectPath:    proto.String(directPath),
			MediaKey:      row.MediaKey,
			FileSHA256:    row.FileSHA256,
			FileEncSHA256: row.FileEncSHA256,
			FileLength:    proto.Uint64(row.FileLength),
		}
	case "sticker":
		quoted.StickerMessage = &waProto.StickerMessage{
			Mimetype:      proto.String("image/webp"),
			URL:           proto.String(row.URL),
			DirectPath:    proto.String(directPath),
			MediaKey:      row.MediaKey,
			FileSHA256:    row.FileSHA256,
			FileEncSHA256: row.FileEncSHA256,
			FileLength:    proto.Uint64(row.FileLength),
		}
	case "video":
		quoted.VideoMessage = &waProto.VideoMessage{
			Caption:       proto.String(row.Content),
			Mimetype:      proto.String("video/mp4"),
			URL:           proto.String(row.URL),
			DirectPath:    proto.String(directPath),
			MediaKey:      row.MediaKey,
			FileSHA256:    row.FileSHA256,
			FileEncSHA256: row.FileEncSHA256,
			FileLength:    proto.Uint64(row.FileLength),
		}
	default:
		// Documents and anything we do not model: quote the text we have. The bubble
		// shows the caption rather than a file card, which beats not replying at all.
		quoted.Conversation = proto.String(row.Content)
	}

	return &waProto.ContextInfo{
		StanzaID:      proto.String(quotedID),
		Participant:   proto.String(participant.String()),
		QuotedMessage: quoted,
	}, nil
}

// Function to send a WhatsApp message.
// Returns the server-assigned message ID and timestamp so the caller can store the
// message locally: WhatsApp does not echo back messages we send ourselves.
func sendWhatsAppMessage(client *whatsmeow.Client, recipient string, message string, mediaPath string,
	ctxInfo *waProto.ContextInfo) (bool, string, string, time.Time) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp", "", time.Time{}
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err), "", time.Time{}
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err), "", time.Time{}
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string
		var isSticker bool

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			// A .webp is ALWAYS a sticker here. Nobody in this chat sends a .webp
			// meaning a photo, and picking the extension as the trigger keeps the whole
			// feature inside this file: no extra field in the JSON, and no change to the
			// Python MCP layer — which would force a Claude Code restart and kill the
			// bot's own session. To send a webp as a photo, convert it to png first.
			isSticker = true
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err), "", time.Time{}
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch {
		case isSticker:
			// Width and Height are not optional: without them the receiving client
			// draws the sticker at some default size, or does not draw it at all. They
			// are read from the WebP header rather than shelling out to ffprobe.
			//
			// No Caption field exists on a sticker — whatever needs saying goes in a
			// separate message.
			w, h, animated := webpSize(mediaData)
			msg.StickerMessage = &waProto.StickerMessage{
				ContextInfo:       ctxInfo,
				Mimetype:          proto.String(mimeType),
				URL:               &resp.URL,
				DirectPath:        &resp.DirectPath,
				MediaKey:          resp.MediaKey,
				FileEncSHA256:     resp.FileEncSHA256,
				FileSHA256:        resp.FileSHA256,
				FileLength:        &resp.FileLength,
				Width:             proto.Uint32(w),
				Height:            proto.Uint32(h),
				IsAnimated:        proto.Bool(animated),
				MediaKeyTimestamp: proto.Int64(time.Now().Unix()),
			}
		case mediaType == whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				ContextInfo:   ctxInfo,
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case mediaType == whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err), "", time.Time{}
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				ContextInfo:   ctxInfo,
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case mediaType == whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				ContextInfo:   ctxInfo,
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case mediaType == whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				ContextInfo:   ctxInfo,
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else if ctxInfo == nil {
		// No quote: keep the exact same wire format as always. A Conversation cannot
		// carry a ContextInfo, which is why the reply case needs the other type.
		msg.Conversation = proto.String(message)
	} else {
		msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
			Text:        proto.String(message),
			ContextInfo: ctxInfo,
		}
	}

	// Send message
	resp, err := client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err), "", time.Time{}
	}

	return true, fmt.Sprintf("Message sent to %s", recipient), string(resp.ID), resp.Timestamp
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", mediaFilename("image", img.GetFileSHA256(), ".jpg"),
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", mediaFilename("video", vid.GetFileSHA256(), ".mp4"),
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", mediaFilename("audio", aud.GetFileSHA256(), ".ogg"),
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		// El nombre que puso quien lo mando se respeta: es informacion suya, no nuestra.
		// Solo se inventa uno cuando el mensaje no trae ninguno.
		filename := doc.GetFileName()
		if filename == "" {
			filename = mediaFilename("document", doc.GetFileSHA256(), "")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	// Check for sticker message. Until this existed, stickers were dropped before ever
	// reaching the database: extractMediaInfo returned an empty media type, and
	// handleMessage discards anything with neither text nor media. Not one of the 142k
	// stored messages was a sticker.
	//
	// whatsmeow already decrypts stickers through the image pipeline (classToMediaType
	// maps StickerMessage to MediaImage), so storing them with their own media_type is
	// all it takes to be able to download them later.
	if stk := msg.GetStickerMessage(); stk != nil {
		return "sticker", stickerFilename(stk.GetFileSHA256()),
			stk.GetURL(), stk.GetMediaKey(), stk.GetFileSHA256(), stk.GetFileEncSHA256(), stk.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Media filenames come from the file hash, not from the clock, for two reasons.
//
// Correctness: two files received within the same second would get the same timestamp
// name, and downloadMedia short-circuits when the file already exists — so the second one
// would silently return the first one's content. Asking for one audio and getting a
// different one back, with no error, is the worst kind of bug: the transcription looks
// perfectly fine and belongs to somebody else's message.
//
// Cost: the same file arrives many times (stickers especially). Hashing means it is
// stored and downloaded exactly once, and the bot's cached description or transcription
// is reused instead of processing the same content again.
//
// 2026-08-16: this used to apply to stickers only, and the rest kept the clock. It cost
// 21k historical audios and 755 recent ones sharing names — one single name was shared by
// 478 different audios. Now every media type goes through here.
//
// Four bytes are enough: measured over the 35k media rows in this database, 29,824 distinct
// hashes produce 29,824 distinct 4-byte prefixes, i.e. zero collisions. Files that DO share
// a hash are the same file forwarded again, and sharing a name is correct for those.
func mediaFilename(prefix string, sha []byte, ext string) string {
	if len(sha) >= 4 {
		return fmt.Sprintf("%s_%x%s", prefix, sha[:4], ext)
	}
	// Sin hash no hay nada mejor que el reloj. Pasa en ~200 filas de 35k.
	return prefix + "_" + time.Now().Format("20060102_150405") + ext
}

func stickerFilename(sha []byte) string {
	return mediaFilename("sticker", sha, ".webp")
}

// webpSize reads dimensions and the animation flag straight out of the WebP header.
//
// Layout: "RIFF" <4 byte size> "WEBP" <4 byte chunk id> <chunk payload>, where the chunk
// id decides how the size is encoded:
//
//	VP8  (lossy)      14-bit width at offset 26, height at 28, little endian
//	VP8L (lossless)   width-1 and height-1 packed as 14+14 bits in the LE uint32 at 21
//	VP8X (extended)   24-bit width-1 at 24 and height-1 at 27; ANIM is bit 0x02 at 20
//
// Falls back to 512x512, which is what WhatsApp normalizes every sticker to anyway, so a
// header this cannot parse still sends something that renders correctly.
func webpSize(data []byte) (width, height uint32, animated bool) {
	width, height = 512, 512

	if len(data) < 30 || string(data[0:4]) != "RIFF" || string(data[8:12]) != "WEBP" {
		return width, height, false
	}

	switch string(data[12:16]) {
	case "VP8 ":
		w := uint32(binary.LittleEndian.Uint16(data[26:28]) & 0x3FFF)
		h := uint32(binary.LittleEndian.Uint16(data[28:30]) & 0x3FFF)
		if w > 0 && h > 0 {
			width, height = w, h
		}
	case "VP8L":
		bits := binary.LittleEndian.Uint32(data[21:25])
		w := (bits & 0x3FFF) + 1
		h := ((bits >> 14) & 0x3FFF) + 1
		if w > 0 && h > 0 {
			width, height = w, h
		}
	case "VP8X":
		animated = data[20]&0x02 != 0
		// Los parentesis importan: en Go el + liga mas fuerte que el |, asi que sin
		// ellos el +1 se sumaria solo al ultimo byte en vez de al valor entero.
		w := (uint32(data[24]) | uint32(data[25])<<8 | uint32(data[26])<<16) + 1
		h := (uint32(data[27]) | uint32(data[28])<<8 | uint32(data[29])<<16) + 1
		width, height = w, h
	}

	return width, height, animated
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// WhatsApp can replay a live event after reconnecting. Keep updating the stored row,
	// but wake Codex only for the first successful insert of this message id.
	alreadyStored, lookupErr := messageStore.HasMessage(msg.Info.ID, chatJID)
	if lookupErr != nil {
		logger.Warnf("Failed to check whether message is new: %v", lookupErr)
	}

	// Store message in database
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Log based on message type
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}

		// A failed lookup is not treated as new: the next real event rechecks the cursor
		// backlog, while a false duplicate wake spends tokens and may double-reply.
		notifyIncomingMessage(
			chatJID,
			msg.Info.ID,
			msg.Info.IsFromMe,
			lookupErr == nil && !alreadyStored,
			msg.Info.IsFromMe && repliesToTulpa(messageStore, chatJID, msg.Message),
			logger,
		)
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// QuotedRow is everything needed to rebuild a message we are about to quote.
type QuotedRow struct {
	Sender        string
	Content       string
	IsFromMe      bool
	MediaType     string
	URL           string
	MediaKey      []byte
	FileSHA256    []byte
	FileEncSHA256 []byte
	FileLength    uint64
}

// GetQuotedInfo reads back a message so it can be quoted. It returns sql.ErrNoRows when
// the ID is not in the database, and the caller must treat that as fatal: sending a
// quote for an ID we do not have is exactly how a reply ends up pointing at nothing.
func (store *MessageStore) GetQuotedInfo(id, chatJID string) (*QuotedRow, error) {
	var q QuotedRow
	var mediaType, url sql.NullString
	err := store.db.QueryRow(
		`SELECT sender, content, is_from_me, media_type, url, media_key, file_sha256, file_enc_sha256, file_length
		 FROM messages WHERE id = ? AND chat_jid = ?`,
		id, chatJID,
	).Scan(&q.Sender, &q.Content, &q.IsFromMe, &mediaType, &url,
		&q.MediaKey, &q.FileSHA256, &q.FileEncSHA256, &q.FileLength)
	if err != nil {
		return nil, err
	}
	q.MediaType = mediaType.String
	q.URL = url.String
	return &q, nil
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := fmt.Sprintf("store/%s", strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	localPath = fmt.Sprintf("%s/%s", chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, filename, absPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	case "sticker":
		// Not a typo: stickers travel through the image media pipeline. whatsmeow maps
		// StickerMessage to MediaImage in both directions, so the decryption keys are
		// derived the same way.
		waMediaType = whatsmeow.MediaImage
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int) {
	// Handler for sending messages
	http.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}

		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		fmt.Println("Received request to send message", req.Message, req.MediaPath)

		// A quote is rebuilt from the database (see buildQuoteContext). A quoted_id we
		// cannot resolve is a hard 404 and never a quiet fallback to an unquoted
		// message: replying anyway would read as if the bot ignored what it answered,
		// and nothing downstream would notice.
		var ctxInfo *waProto.ContextInfo
		if req.QuotedID != "" {
			chatJID := req.Recipient
			if !strings.Contains(chatJID, "@") {
				chatJID = chatJID + "@s.whatsapp.net"
			}
			parsed, err := types.ParseJID(chatJID)
			if err != nil || parsed.User == "" || parsed.Server == "" {
				http.Error(w, fmt.Sprintf("Invalid recipient JID: %q", req.Recipient), http.StatusBadRequest)
				return
			}
			ctxInfo, err = buildQuoteContext(client, messageStore, parsed, req.QuotedID)
			if err != nil {
				http.Error(w, err.Error(), http.StatusNotFound)
				return
			}
			fmt.Println("Quoting", req.QuotedID)
		}

		// Send the message
		success, message, msgID, msgTime := sendWhatsAppMessage(client, req.Recipient, req.Message, req.MediaPath, ctxInfo)
		fmt.Println("Message sent", success, message)
		if success {
			rememberSentByAPI(msgID)
		}

		// Store our own outgoing message: WhatsApp never echoes it back to us, so
		// without this the local history has holes exactly where we spoke.
		// sender is "__tulpa__" to tell messages sent through this API apart from
		// the ones the human sends from the phone (those arrive via sync).
		if success && msgID != "" {
			chatJID := req.Recipient
			if !strings.Contains(chatJID, "@") {
				chatJID = chatJID + "@s.whatsapp.net"
			}
			mediaType := ""
			filename := ""
			if req.MediaPath != "" {
				mediaType = mediaTypeFromPath(req.MediaPath)
				filename = filepath.Base(req.MediaPath)
			}
			if err := messageStore.StoreMessage(msgID, chatJID, "__tulpa__", req.Message,
				msgTime, true, mediaType, filename, "", nil, nil, nil, 0); err != nil {
				fmt.Printf("Warning: could not store outgoing message: %v\n", err)
			}
		}

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Set appropriate status code
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}

		// Send response
		json.NewEncoder(w).Encode(SendMessageResponse{
			Success:   success,
			Message:   message,
			MessageID: msgID,
		})
	})

	// Handler for downloading media
	http.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// Handler for health checks.
	//
	// Answering "is the port open?" is not enough: the process can be alive, listening
	// and disconnected from WhatsApp at the same time, which is exactly the state left
	// behind by a replaced stream. IsConnected and IsLoggedIn are different failures and
	// need different fixes — a logged out session is not solved by restarting, it needs
	// the QR scanned again — so both are reported separately.
	// Handler for sending read receipts — the blue double check on her side.
	//
	// Which IDs to mark is decided by the caller (pulso.sh) with SQL, the same way every
	// other query in this project lives in the scripts. This endpoint only validates.
	http.HandleFunc("/api/markread", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req MarkReadRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.ChatJID == "" {
			http.Error(w, "chat_jid is required", http.StatusBadRequest)
			return
		}
		if len(req.MessageIDs) == 0 {
			http.Error(w, "message_ids is required", http.StatusBadRequest)
			return
		}
		// A single receipt node carries every ID, so an unbounded list would build one
		// huge stanza after a long silence. The caller caps this too; this is the floor.
		if len(req.MessageIDs) > 200 {
			http.Error(w, "too many message_ids (max 200)", http.StatusBadRequest)
			return
		}

		chat, err := types.ParseJID(req.ChatJID)
		// ParseJID accepts almost anything: a string with no "@" comes back as an empty
		// User with the whole string as Server, and no error. Checking the parts is the
		// only way to reject junk before sending a receipt into the void.
		if err != nil || chat.User == "" || chat.Server == "" {
			http.Error(w, fmt.Sprintf("Invalid chat_jid: %q", req.ChatJID), http.StatusBadRequest)
			return
		}

		ids := make([]types.MessageID, 0, len(req.MessageIDs))
		for _, id := range req.MessageIDs {
			if id != "" {
				ids = append(ids, types.MessageID(id))
			}
		}
		if len(ids) == 0 {
			http.Error(w, "message_ids is required", http.StatusBadRequest)
			return
		}

		// EmptyJID as the sender is correct rather than a shortcut: whatsmeow only
		// attaches a participant when the chat server is not a DM one, and this chat is
		// @lid (types.HiddenUserServer), so the argument is ignored. See receipt.go.
		// It also sidesteps the sender column not storing a server.
		err = client.MarkRead(context.Background(), ids, time.Now(), chat, types.EmptyJID)

		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(MarkReadResponse{
				Success: false,
				Message: fmt.Sprintf("Error marking read: %v", err),
			})
			return
		}
		json.NewEncoder(w).Encode(MarkReadResponse{
			Success: true,
			Message: fmt.Sprintf("Marked %d message(s) as read in %s", len(ids), chat),
			Marked:  len(ids),
		})
	})

	http.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		last := lastEventUnix.Load()
		ago := int64(-1)
		if last > 0 {
			ago = time.Now().Unix() - last
		}
		// Cached client-side by whatsmeow, so this costs no round trip per poll.
		readReceipts := ""
		if client.IsLoggedIn() {
			readReceipts = string(client.GetPrivacySettings(context.Background()).ReadReceipts)
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(HealthResponse{
			Connected:     client.IsConnected(),
			LoggedIn:      client.IsLoggedIn(),
			UptimeS:       int64(time.Since(startedAt).Seconds()),
			LastEventAgoS: ago,
			ReadReceipts:  readReceipts,
		})
	})

	// Start the server
	serverAddr := fmt.Sprintf(":%d", port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, nil); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
			// Fatal on purpose. A busy port means another bridge is already running,
			// and two clients on one session make WhatsApp drop one of them. Dying here
			// is better than staying connected as a zombie with no API: that is exactly
			// the state where the bot keeps reasoning on a database nobody is feeding.
			os.Exit(1)
		}
	}()
}

func main() {
	// Set up logger
	logger := waLog.Stdout("Client", "INFO", true)

	// This binary takes no arguments: the engine flags belong to bin/wspbot. Silently
	// ignoring them is how `wspbot --claude` ran on 2026-08-25 with the saved engine
	// still on codex - every turn died against the ChatGPT writer lock and nothing said
	// why. A stale shell alias pointing straight at this binary is exactly how that
	// happens, so refuse instead of swallowing it.
	if len(os.Args) > 1 {
		fmt.Fprintf(os.Stderr, "whatsapp-client no acepta argumentos: %v\n", os.Args[1:])
		fmt.Fprintln(os.Stderr, "los flags del motor son de wspbot:  wspbot --claude | --codex")
		fmt.Fprintln(os.Stderr, "si acabas de cambiar el alias, abre una terminal nueva o:  unalias wspbot")
		os.Exit(2)
	}
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	// Refuse to start if another bridge is already up, before touching the session.
	lock, err := acquireLock()
	if err != nil {
		logger.Errorf("Refusing to start: %v", err)
		logger.Errorf("Two clients on one session make WhatsApp drop one of them.")
		return
	}
	defer lock.Close()

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()
	startEventWatcher(logger)

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		// Stamped on every event, not just messages: /api/health reports how long ago
		// anything at all arrived, which is what tells a quiet chat apart from a dead
		// connection that has not noticed it is dead yet.
		lastEventUnix.Store(time.Now().Unix())

		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")
		}
	})

	// Create channel to track connection success
	connected := make(chan bool, 1)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		// No ID stored, this is a new client, need to pair with phone
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}

		// Print QR code for pairing with phone
		for evt := range qrChan {
			if evt.Event == "code" {
				fmt.Println("\nScan this QR code with your WhatsApp app:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			} else if evt.Event == "success" {
				connected <- true
				break
			}
		}

		// Wait for connection
		select {
		case <-connected:
			fmt.Println("\nSuccessfully connected and authenticated!")
		case <-time.After(3 * time.Minute):
			logger.Errorf("Timeout waiting for QR code scan")
			return
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		connected <- true
	}

	// Wait a moment for connection to stabilize
	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}

	fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")

	// Start REST API server
	startRESTServer(client, messageStore, 8080)

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that
		logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		// Just use contact info (full name)
		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err == nil && contact.FullName != "" {
			name = contact.FullName
		} else if sender != "" {
			// Fallback to sender
			name = sender
		} else {
			// Last fallback to JID
			name = jid.User
		}

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Extract text content
				var content string
				if msg.Message.Message != nil {
					if conv := msg.Message.Message.GetConversation(); conv != "" {
						content = conv
					} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
						content = ext.GetText()
					}
				}

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
				}

				// Log the message content for debugging
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender
				var sender string
				isFromMe := false
				if msg.Message.Key != nil {
					if msg.Message.Key.FromMe != nil {
						isFromMe = *msg.Message.Key.FromMe
					}
					if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
						sender = *msg.Message.Key.Participant
					} else if isFromMe {
						sender = client.Store.ID.User
					} else {
						sender = jid.User
					}
				} else {
					sender = jid.User
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					// Log successful message storage
					if mediaType != "" {
						logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
					} else {
						logger.Infof("Stored message: [%s] %s -> %s: %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, content)
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// Request history sync from the server
func requestHistorySync(client *whatsmeow.Client) {
	if client == nil {
		fmt.Println("Client is not initialized. Cannot request history sync.")
		return
	}

	if !client.IsConnected() {
		fmt.Println("Client is not connected. Please ensure you are connected to WhatsApp first.")
		return
	}

	if client.Store.ID == nil {
		fmt.Println("Client is not logged in. Please scan the QR code first.")
		return
	}

	// Build and send a history sync request
	historyMsg := client.BuildHistorySyncRequest(nil, 100)
	if historyMsg == nil {
		fmt.Println("Failed to build history sync request.")
		return
	}

	_, err := client.SendMessage(context.Background(), types.JID{
		Server: "s.whatsapp.net",
		User:   "status",
	}, historyMsg)

	if err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
	} else {
		fmt.Println("History sync requested. Waiting for server response...")
	}
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
