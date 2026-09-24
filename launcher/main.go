// aihub launcher: a single executable that carries a private Python runtime plus the aihub
// package, unpacks them once into the user's app-data folder and starts the web UI.
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
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
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
	fmt.Println("Первый запуск: распаковываю aihub, это займёт несколько секунд…")
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

func fail(msg string, err error) {
	fmt.Printf("\n%s: %v\n", msg, err)
	fmt.Println("Нажми Enter, чтобы закрыть окно.")
	bufio.NewReader(os.Stdin).ReadString('\n')
	os.Exit(1)
}

func main() {
	dir, err := ensureRuntime()
	if err != nil {
		fail("Не получилось распаковать aihub", err)
	}

	python := filepath.Join(dir, "python.exe")
	env := []string{}
	for _, kv := range os.Environ() {
		k := strings.ToUpper(strings.SplitN(kv, "=", 2)[0])
		if k == "PYTHONHOME" || k == "PYTHONPATH" || k == "PYTHONSTARTUP" {
			continue // don't let a separately installed Python interfere
		}
		env = append(env, kv)
	}
	env = append(env, "PYTHONUTF8=1", "PYTHONNOUSERSITE=1")
	if runtime.GOOS != "windows" { // dev/test builds: use the system python with our package
		python = "python3"
		env = append(env, "PYTHONPATH="+filepath.Join(dir, "Lib", "site-packages"))
	}

	cmd := exec.Command(python, append([]string{"-m", "aihub", "ui"}, os.Args[1:]...)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.Env = env
	if home, err := os.UserHomeDir(); err == nil {
		cmd.Dir = home
	}
	// Ctrl+C goes to Python too; the launcher just waits for it to shut down.
	signal.Ignore(os.Interrupt)
	if err := cmd.Run(); err != nil {
		if _, ok := err.(*exec.ExitError); ok && cmd.ProcessState.ExitCode() == 0xC000013A {
			return // STATUS_CONTROL_C_EXIT
		}
		fail("aihub завершился с ошибкой", err)
	}
}
