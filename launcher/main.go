// aihub launcher: a single executable that carries a private Python runtime plus the aihub
// package, unpacks them once into the user's app-data folder, starts the aihub server in the
// background and shows it in a window (Windows: a native WebView2 window, see app_windows.go).
// Built by build.sh; payload.zip is generated there and not committed.
package main

import (
	"archive/zip"
	"bufio"
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

//go:embed payload.zip
var payload []byte

func dataDir() string {
	if runtime.GOOS == "windows" {
		if d := os.Getenv("LOCALAPPDATA"); d != "" {
			return filepath.Join(d, "aihub")
		}
	}
	if d, err := os.UserCacheDir(); err == nil {
		return filepath.Join(d, "aihub")
	}
	return filepath.Join(os.TempDir(), "aihub")
}

func unzip(data []byte, dest string) error {
	r, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return err
	}
	root := filepath.Clean(dest) + string(os.PathSeparator)
	for _, f := range r.File {
		target := filepath.Join(dest, filepath.FromSlash(f.Name))
		if !strings.HasPrefix(target, root) {
			return fmt.Errorf("bad path in payload: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		src, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, f.Mode()|0o600)
		if err != nil {
			src.Close()
			return err
		}
		_, err = io.Copy(out, src)
		src.Close()
		if cerr := out.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// ensureRuntime unpacks the payload into a folder named after its hash, so a new version of
// the exe gets a fresh copy and an interrupted unpack is never used.
func ensureRuntime() (string, error) {
	sum := sha256.Sum256(payload)
	base := dataDir()
	dir := filepath.Join(base, "runtime-"+hex.EncodeToString(sum[:6]))
	if _, err := os.Stat(filepath.Join(dir, ".complete")); err == nil {
		return dir, nil
	}
	tmp := fmt.Sprintf("%s.tmp-%d", dir, os.Getpid())
	os.RemoveAll(tmp)
	if err := unzip(payload, tmp); err != nil {
		os.RemoveAll(tmp)
		return "", err
	}
	if err := os.WriteFile(filepath.Join(tmp, ".complete"), nil, 0o644); err != nil {
		return "", err
	}
	os.RemoveAll(dir)
	if err := os.Rename(tmp, dir); err != nil {
		os.RemoveAll(tmp)
		if _, statErr := os.Stat(filepath.Join(dir, ".complete")); statErr != nil {
			return "", err
		} // another copy of the launcher finished first — use its result
	}
	// Drop runtimes left by older versions (best effort; may be in use).
	if old, _ := filepath.Glob(filepath.Join(base, "runtime-*")); old != nil {
		for _, o := range old {
			if o != dir && !strings.Contains(filepath.Base(o), ".tmp-") {
				os.RemoveAll(o)
			}
		}
	}
	return dir, nil
}

// server is the background aihub process.
type server struct {
	cmd   *exec.Cmd
	url   string
	token string
	log   string
}

// startServer runs `python -m aihub ui --announce` hidden and waits for it to print its
// address. Everything the server prints goes to aihub.log in the data folder.
func startServer(dir string, extraArgs []string, prepare func(*exec.Cmd)) (*server, error) {
	python := filepath.Join(dir, "python.exe")
	env := []string{}
	for _, kv := range os.Environ() {
		k := strings.ToUpper(strings.SplitN(kv, "=", 2)[0])
		if k == "PYTHONHOME" || k == "PYTHONPATH" || k == "PYTHONSTARTUP" {
			continue // don't let a separately installed Python interfere
		}
		env = append(env, kv)
	}
	env = append(env, "PYTHONUTF8=1", "PYTHONNOUSERSITE=1", "PYTHONUNBUFFERED=1")
	if runtime.GOOS != "windows" { // dev/test builds: use the system python with our package
		python = "python3"
		env = append(env, "PYTHONPATH="+filepath.Join(dir, "Lib", "site-packages"))
	}

	args := append([]string{"-m", "aihub", "ui", "--announce", "--no-browser", "--port", "0",
		"--idle-exit", "600"}, extraArgs...)
	cmd := exec.Command(python, args...)
	cmd.Env = env
	if home, err := os.UserHomeDir(); err == nil {
		cmd.Dir = home
	}
	logPath := filepath.Join(dataDir(), "aihub.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		return nil, err
	}
	cmd.Stderr = logFile
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if prepare != nil {
		prepare(cmd)
	}
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return nil, err
	}

	s := &server{cmd: cmd, log: logPath}
	ready := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(stdout)
		for sc.Scan() {
			line := sc.Text()
			switch {
			case strings.HasPrefix(line, "AIHUB_TOKEN "):
				s.token = strings.TrimPrefix(line, "AIHUB_TOKEN ")
			case strings.HasPrefix(line, "AIHUB_URL ") && s.url == "":
				s.url = strings.TrimPrefix(line, "AIHUB_URL ")
				fmt.Fprintln(logFile, line)
				close(ready)
			default:
				fmt.Fprintln(logFile, line)
			}
		}
		logFile.Close()
	}()
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()

	select {
	case <-ready:
		go func() { <-exited }() // reap
		return s, nil
	case err := <-exited:
		return nil, fmt.Errorf("server stopped: %v\n\n%s", err, tail(logPath))
	case <-time.After(90 * time.Second):
		cmd.Process.Kill()
		return nil, fmt.Errorf("server did not start in time\n\n%s", tail(logPath))
	}
}

// stop asks the server to cancel running agents and exit, then makes sure it is gone.
func (s *server) stop() {
	if s.token != "" {
		req, _ := http.NewRequest("POST", s.url+"api/shutdown", strings.NewReader("{}"))
		req.Header.Set("X-Aihub-Token", s.token)
		req.Header.Set("Content-Type", "application/json")
		client := http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{Proxy: nil}}
		if resp, err := client.Do(req); err == nil {
			resp.Body.Close()
		}
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) && s.cmd.ProcessState == nil {
			time.Sleep(100 * time.Millisecond)
		}
	}
	if s.cmd.ProcessState == nil {
		s.cmd.Process.Kill()
	}
}

func tail(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) > 15 {
		lines = lines[len(lines)-15:]
	}
	return strings.Join(lines, "\n")
}
