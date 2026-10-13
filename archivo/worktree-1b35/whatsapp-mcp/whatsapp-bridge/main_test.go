package main

import "testing"

func TestShouldTriggerIncoming(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "/tmp/evento-wsp")
	t.Setenv("WSP_JID", "objetivo@lid")

	tests := []struct {
		name     string
		chatJID  string
		fromMe   bool
		isNew    bool
		expected bool
	}{
		{"incoming new target", "objetivo@lid", false, true, true},
		{"other chat", "otro@lid", false, true, false},
		{"outgoing", "objetivo@lid", true, true, false},
		{"replayed", "objetivo@lid", false, false, false},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := shouldTriggerIncoming(test.chatJID, test.fromMe, test.isNew)
			if got != test.expected {
				t.Fatalf("shouldTriggerIncoming() = %v, want %v", got, test.expected)
			}
		})
	}
}

func TestShouldTriggerCanBeDisabled(t *testing.T) {
	t.Setenv("WSP_EVENT_TRIGGER", "")
	t.Setenv("WSP_JID", "objetivo@lid")
	if shouldTriggerIncoming("objetivo@lid", false, true) {
		t.Fatal("empty WSP_EVENT_TRIGGER must disable event wakes")
	}
}
