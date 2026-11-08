// Linux 会话录制进程。只监听回环地址；控制面经 stdin/stdout，不安装系统 CA。
package main

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"hash"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/lqqyt2423/go-mitmproxy/cert"
)

const bodyLimit = 32 << 20

type config struct{ Root, Secret, Upstream string }
type bodyInfo struct {
	File      string `json:"file"`
	Bytes     int64  `json:"bytes"`
	Total     int64  `json:"total"`
	Complete  bool   `json:"complete"`
	Truncated bool   `json:"truncated"`
	SHA256    string `json:"sha256"`
	Error     string `json:"error,omitempty"`
}
type recorder struct {
	mu     sync.Mutex
	closed bool
	file   *os.File
	info   bodyInfo
	sum    hash.Hash
}

func newRecorder(root, name string) *recorder {
	f, err := os.OpenFile(filepath.Join(root, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	r := &recorder{file: f, info: bodyInfo{File: name}, sum: sha256.New()}
	if err != nil {
		r.info.Error = "capture_write_failed"
	}
	return r
}
func (r *recorder) Write(data []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return len(data), nil
	}
	r.info.Total += int64(len(data))
	keep := min(int64(len(data)), max(int64(0), bodyLimit-r.info.Bytes))
	if r.file != nil && keep > 0 {
		n, err := r.file.Write(data[:keep])
		r.info.Bytes += int64(n)
		r.sum.Write(data[:n])
		if err != nil {
			r.info.Error = "capture_write_failed"
			r.file.Close()
			r.file = nil
		}
	}
	r.info.Truncated = r.info.Total > r.info.Bytes
	return len(data), nil // 证据写入失败不能中断目标流量。
}
func (r *recorder) finish(complete bool) bodyInfo {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed = true
	if r.file != nil {
		if r.file.Close() != nil {
			r.info.Error = "capture_close_failed"
		}
	}
	r.info.Complete = complete && !r.info.Truncated && r.info.Error == ""
	r.info.SHA256 = hex.EncodeToString(r.sum.Sum(nil))
	return r.info
}

type readCapture struct {
	io.ReadCloser
	sink *recorder
	eof  atomic.Bool
}

func (r *readCapture) Read(p []byte) (int, error) {
	n, err := r.ReadCloser.Read(p)
	if n > 0 {
		r.sink.Write(p[:n])
	}
	if err == io.EOF {
		r.eof.Store(true)
	}
	return n, err
}

type engine struct {
	conf      config
	ca        cert.CA
	transport *http.Transport
	output    sync.Mutex
	conns     sync.Map
	work      sync.RWMutex
	closing   bool
}

func (e *engine) emit(v any) {
	e.output.Lock()
	defer e.output.Unlock()
	_ = json.NewEncoder(os.Stdout).Encode(v)
}
func id() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}
func headers(h http.Header, host string) [][2]string {
	rows := [][2]string{}
	remaining := 262144
	if host != "" {
		rows = append(rows, [2]string{"Host", host})
	}
	for key, values := range h {
		if strings.EqualFold(key, "Proxy-Authorization") {
			continue
		}
		for _, value := range values {
			if remaining <= 0 {
				return rows
			}
			value = value[:min(len(value), remaining)]
			remaining -= len(key) + len(value)
			rows = append(rows, [2]string{key, value})
		}
	}
	return rows
}
func stripHop(h http.Header, upgrade bool) {
	for _, key := range strings.Split(h.Get("Connection"), ",") {
		key = strings.TrimSpace(key)
		if !upgrade || !strings.EqualFold(key, "Upgrade") {
			h.Del(key)
		}
	}
	for _, key := range []string{"Proxy-Authorization", "Proxy-Connection", "Keep-Alive", "TE", "Trailer", "Transfer-Encoding"} {
		h.Del(key)
	}
	if !upgrade {
		h.Del("Connection")
		h.Del("Upgrade")
	}
}
func (e *engine) auth(w http.ResponseWriter, r *http.Request) (string, bool) {
	raw := strings.TrimPrefix(r.Header.Get("Proxy-Authorization"), "Basic ")
	decoded, err := base64.StdEncoding.DecodeString(raw)
	user, secret, ok := strings.Cut(string(decoded), ":")
	if err != nil || !ok || len(user) > 100 || subtle.ConstantTimeCompare([]byte(secret), []byte(e.conf.Secret)) != 1 {
		w.Header().Set("Proxy-Authenticate", `Basic realm="SpiritScout session"`)
		http.Error(w, "Proxy authentication required", 407)
		return "", false
	}
	r.Header.Del("Proxy-Authorization")
	return user, true
}
func (e *engine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	user, ok := e.auth(w, r)
	if !ok {
		return
	}
	if r.Method == "CONNECT" {
		e.connect(w, r, user)
		return
	}
	if r.URL.Scheme != "http" || r.URL.Host == "" {
		http.Error(w, "Absolute HTTP URL required", 400)
		return
	}
	e.forward(w, r, user, "HTTP")
}

// 一个 CONNECT 中的 HTTP 请求共享原连接身份，不把归属头转发到目标。
func (e *engine) connect(w http.ResponseWriter, r *http.Request, user string) {
	host, _, err := net.SplitHostPort(r.Host)
	if err != nil {
		http.Error(w, "Invalid CONNECT authority", 400)
		return
	}
	client, buffer, err := w.(http.Hijacker).Hijack()
	if err != nil {
		return
	}
	e.conns.Store(client, true)
	defer e.conns.Delete(client)
	defer client.Close()
	_, _ = buffer.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n")
	if buffer.Flush() != nil {
		return
	}
	_ = client.SetDeadline(time.Now().Add(20 * time.Second))
	first, err := buffer.Reader.Peek(1)
	if err != nil {
		return
	}
	var inner net.Conn = &bufferedConn{Conn: client, reader: buffer.Reader}
	scheme := "http"
	if first[0] == 0x16 {
		scheme = "https"
		secured := tls.Server(inner, &tls.Config{
			MinVersion: tls.VersionTLS12, NextProtos: []string{"http/1.1"},
			GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
				name := hello.ServerName
				if name == "" {
					name = host
				}
				return e.ca.GetCert(name)
			},
		})
		if secured.Handshake() != nil {
			return
		}
		inner = secured
	}
	_ = inner.SetDeadline(time.Time{})
	one := &oneListener{conn: inner, done: make(chan struct{})}
	server := &http.Server{Handler: http.HandlerFunc(func(out http.ResponseWriter, req *http.Request) {
		req.URL.Scheme = scheme
		req.URL.Host = r.Host
		e.forward(out, req, user, "HTTP")
	}), ReadHeaderTimeout: 20 * time.Second}
	_ = server.Serve(one)
}

type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

type oneListener struct {
	conn net.Conn
	done chan struct{}
	sent bool
	once sync.Once
}

func (l *oneListener) Accept() (net.Conn, error) {
	if !l.sent {
		l.sent = true
		return &closeConn{Conn: l.conn, close: l.Close}, nil
	}
	<-l.done
	return nil, net.ErrClosed
}
func (l *oneListener) Close() error   { l.once.Do(func() { close(l.done) }); return nil }
func (l *oneListener) Addr() net.Addr { return l.conn.LocalAddr() }

type closeConn struct {
	net.Conn
	close func() error
}

func (c *closeConn) Close() error { err := c.Conn.Close(); _ = c.close(); return err }

func (e *engine) forward(w http.ResponseWriter, r *http.Request, user, protocol string) {
	e.work.RLock()
	if e.closing {
		e.work.RUnlock()
		http.Error(w, "Proxy stopping", 503)
		return
	}
	defer e.work.RUnlock()
	key := id()
	started := time.Now()
	reqSink := newRecorder(e.conf.Root, key+"-request.bin")
	resSink := newRecorder(e.conf.Root, key+"-response.bin")
	meta := map[string]any{"type": "exchange", "key": key, "context": user, "protocol": protocol, "method": r.Method, "url": r.URL.String(), "started": float64(started.UnixNano()) / 1e9, "request_headers": headers(r.Header, r.Host)}
	request := r.Clone(r.Context())
	request.RequestURI = ""
	request.Header = r.Header.Clone()
	upgrade := strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
	stripHop(request.Header, upgrade)
	input := &readCapture{ReadCloser: r.Body, sink: reqSink}
	request.Body = input
	if r.Body == http.NoBody || r.ContentLength == 0 {
		input.eof.Store(true)
		request.Body = http.NoBody
	}
	response, err := e.transport.RoundTrip(request)
	complete := false
	if err != nil {
		meta["error"] = "UpstreamRequestError"
		http.Error(w, "Upstream request failed", 502)
	} else {
		defer response.Body.Close()
		meta["status"] = response.StatusCode
		meta["response_headers"] = headers(response.Header, "")
		if response.StatusCode == 101 && upgrade {
			meta["protocol"] = "WS"
			complete = e.upgrade(w, r, response, reqSink, resSink)
		} else {
			outHeaders := response.Header.Clone()
			stripHop(outHeaders, false)
			for k, values := range outHeaders {
				for _, v := range values {
					w.Header().Add(k, v)
				}
			}
			w.WriteHeader(response.StatusCode)
			_, err = io.Copy(&flushWriter{out: w}, io.TeeReader(response.Body, resSink))
			complete = err == nil
			if err != nil {
				meta["error"] = "StreamInterrupted"
			}
		}
	}
	reqSink.mu.Lock()
	requestComplete := input.eof.Load() || (r.ContentLength >= 0 && r.ContentLength == reqSink.info.Total)
	reqSink.mu.Unlock()
	meta["request"] = reqSink.finish(requestComplete)
	meta["response"] = resSink.finish(complete)
	meta["duration_ms"] = float64(time.Since(started).Microseconds()) / 1000
	e.emit(meta)
}

type flushWriter struct{ out http.ResponseWriter }

func (w *flushWriter) Write(p []byte) (int, error) {
	n, err := w.out.Write(p)
	if f, ok := w.out.(http.Flusher); ok {
		f.Flush()
	}
	return n, err
}
func (e *engine) upgrade(w http.ResponseWriter, r *http.Request, res *http.Response, request, response *recorder) bool {
	upstream, ok := res.Body.(io.ReadWriteCloser)
	if !ok {
		return false
	}
	client, buffer, err := w.(http.Hijacker).Hijack()
	if err != nil {
		return false
	}
	e.conns.Store(client, true)
	defer e.conns.Delete(client)
	defer client.Close()
	_, _ = buffer.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
	_ = res.Header.Write(buffer)
	_, _ = buffer.WriteString("\r\n")
	if buffer.Flush() != nil {
		return false
	}
	done := make(chan struct{})
	go func() {
		_, _ = io.Copy(upstream, io.TeeReader(buffer.Reader, request))
		_ = upstream.Close()
		close(done)
	}()
	_, err = io.Copy(client, io.TeeReader(upstream, response))
	_ = client.Close()
	_ = upstream.Close()
	<-done
	return err == nil
}
func main() {
	var conf config
	if json.NewDecoder(io.LimitReader(os.Stdin, 65536)).Decode(&conf) != nil || len(conf.Secret) < 32 || conf.Root == "" {
		os.Exit(2)
	}
	if os.MkdirAll(conf.Root, 0700) != nil {
		os.Exit(2)
	}
	ca, err := cert.NewSelfSignCAMemory()
	if err != nil {
		os.Exit(2)
	}
	caPath := filepath.Join(conf.Root, "ca-cert.pem")
	if os.WriteFile(caPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.GetRootCA().Raw}), 0600) != nil {
		os.Exit(2)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	transport.ResponseHeaderTimeout = 60 * time.Second
	if conf.Upstream != "" {
		parsed, err := url.Parse(conf.Upstream)
		if err != nil || parsed.Host == "" {
			os.Exit(2)
		}
		transport.Proxy = http.ProxyURL(parsed)
	}
	e := &engine{conf: conf, ca: ca, transport: transport}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		os.Exit(2)
	}
	server := &http.Server{Handler: e, ReadHeaderTimeout: 20 * time.Second, ConnState: func(c net.Conn, state http.ConnState) {
		if state == http.StateNew {
			e.conns.Store(c, true)
		}
		if state == http.StateClosed {
			e.conns.Delete(c)
		}
	}}
	e.emit(map[string]any{"type": "ready", "address": "http://" + listener.Addr().String(), "ca_file": caPath})
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	// 控制管道仅由 Python 父进程持有；父进程异常退出时 EOF 也会停止代理。
	go func() {
		_, _ = io.Copy(io.Discard, os.Stdin)
		select {
		case signals <- syscall.SIGTERM:
		default:
		}
	}()
	go func() {
		<-signals
		_ = server.Close()
		e.conns.Range(func(key, value any) bool { _ = key.(net.Conn).Close(); return true })
	}()
	err = server.Serve(listener)
	e.conns.Range(func(key, value any) bool { _ = key.(net.Conn).Close(); return true })
	transport.CloseIdleConnections()
	e.work.Lock()
	e.closing = true
	e.work.Unlock()
	if err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, net.ErrClosed) {
		os.Exit(1)
	}
}
