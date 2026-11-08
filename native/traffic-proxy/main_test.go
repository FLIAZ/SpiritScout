package main

import (
	"bytes"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestCaptureLimitDoesNotLimitForwardedBytes(t *testing.T) {
	root := t.TempDir()
	sink := newRecorder(root, "body.bin")
	block := bytes.Repeat([]byte("x"), 1<<20)
	for range 33 {
		n, err := sink.Write(block)
		if err != nil || n != len(block) {
			t.Fatal("capture changed stream write")
		}
	}
	info := sink.finish(true)
	if info.Total != 33<<20 || info.Bytes != 32<<20 || !info.Truncated || info.Complete {
		t.Fatalf("incorrect completeness: %+v", info)
	}
	stat, _ := os.Stat(filepath.Join(root, "body.bin"))
	if stat.Size() != 32<<20 {
		t.Fatal(stat.Size())
	}
}
func TestCaptureWriteFailureLeavesStreamReadable(t *testing.T) {
	sink := newRecorder(filepath.Join(t.TempDir(), "missing"), "body.bin")
	data := bytes.Repeat([]byte("x"), 1024)
	out, err := io.ReadAll(io.TeeReader(bytes.NewReader(data), sink))
	info := sink.finish(true)
	if err != nil || !bytes.Equal(out, data) || info.Error == "" || info.Complete {
		t.Fatal("capture error affected target bytes")
	}
}
func TestCaptureConcurrentEarlyClose(t *testing.T) {
	sink := newRecorder(t.TempDir(), "body.bin")
	var group sync.WaitGroup
	group.Add(1)
	go func() {
		defer group.Done()
		for range 100 {
			_, _ = sink.Write([]byte("body"))
		}
	}()
	_ = sink.finish(false)
	group.Wait()
}
func TestProxyAuthNeverReachesTarget(t *testing.T) {
	e := &engine{conf: config{Secret: "fixture-secret"}}
	r := httptest.NewRequest("GET", "http://fixture.invalid/", nil)
	r.SetBasicAuth("not-proxy", "wrong")
	w := httptest.NewRecorder()
	if _, ok := e.auth(w, r); ok || w.Code != http.StatusProxyAuthRequired {
		t.Fatal("unauthenticated proxy accepted")
	}
	r.Header.Set("Proxy-Authorization", "Basic Y2FsbDppbnZhbGlk")
	w = httptest.NewRecorder()
	if _, ok := e.auth(w, r); ok {
		t.Fatal("invalid proxy secret accepted")
	}
	r.Header.Set("Proxy-Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte("call:fixture-secret")))
	w = httptest.NewRecorder()
	user, ok := e.auth(w, r)
	if !ok || user != "call" || r.Header.Get("Proxy-Authorization") != "" {
		t.Fatal("proxy credentials not consumed locally")
	}
}
