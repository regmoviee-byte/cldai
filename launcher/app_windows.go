//go:build windows

// Windows desktop app: no console, a native window (WebView2, the engine built into Windows
// 10/11) showing the aihub UI, one instance at a time. Closing the window stops aihub.
package main

import (
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"

	webview2 "github.com/jchv/go-webview2"
	"golang.org/x/sys/windows"
)

const (
	createNoWindow = 0x08000000
	windowTitle    = "aihub"
	iconResourceID = 1 // RT_GROUP_ICON #1, see winres/winres.json
)

var (
	user32              = windows.NewLazySystemDLL("user32.dll")
	procFindWindow      = user32.NewProc("FindWindowW")
	procShowWindow      = user32.NewProc("ShowWindow")
	procSetForeground   = user32.NewProc("SetForegroundWindow")
	procIsIconic        = user32.NewProc("IsIconic")
	loadingPage         = pageHTML("Запускаю aihub…", "Первый запуск занимает несколько секунд.", "")
	errorTitle          = "Не получилось запустить aihub"
	webviewMissingNotes = "Не найден компонент Microsoft Edge WebView2. Открываю aihub в браузере."
)

func utf16(s string) *uint16 {
	p, _ := windows.UTF16PtrFromString(s)
	return p
}

func messageBox(text string) {
	windows.MessageBox(0, utf16(text), utf16(windowTitle), windows.MB_OK|windows.MB_ICONERROR)
}

// focusExisting brings the already open aihub window to the front.
func focusExisting() bool {
	hwnd, _, _ := procFindWindow.Call(uintptr(unsafe.Pointer(utf16("webview"))),
		uintptr(unsafe.Pointer(utf16(windowTitle))))
	if hwnd == 0 {
		return false
	}
	if iconic, _, _ := procIsIconic.Call(hwnd); iconic != 0 {
		procShowWindow.Call(hwnd, 9) // SW_RESTORE
	}
	procSetForeground.Call(hwnd)
	return true
}

func hidden(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
}

func pageHTML(title, text, details string) string {
	d := ""
	if details != "" {
		d = `<pre>` + html.EscapeString(details) + `</pre>`
	}
	return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
:root{color-scheme:light dark}
body{margin:0;height:100vh;display:grid;place-items:center;font:15px/1.5 "Segoe UI",system-ui,sans-serif;
background:#f5f4ef;color:#1f1e1b}
@media (prefers-color-scheme:dark){body{background:#131312;color:#edece7}pre{background:#252421!important}}
.box{max-width:640px;padding:24px;text-align:center}
.spin{width:28px;height:28px;margin:0 auto 16px;border:3px solid #7b8cff55;border-top-color:#3d5afe;border-radius:50%;animation:s .8s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
h1{font-size:20px;margin:0 0 6px}p{margin:0;opacity:.7}
pre{text-align:left;white-space:pre-wrap;word-break:break-word;background:#efede6;padding:12px;border-radius:10px;font-size:12px;max-height:50vh;overflow:auto}
</style></head><body><div class="box">` + func() string {
		if details == "" {
			return `<div class="spin"></div>`
		}
		return ""
	}() + `<h1>` + html.EscapeString(title) + `</h1><p>` + html.EscapeString(text) + `</p>` + d +
		`</div></body></html>`
}

func main() {
	mutex, err := windows.CreateMutex(nil, false, utf16(`Local\aihub-desktop`))
	if err == windows.ERROR_ALREADY_EXISTS {
		for i := 0; i < 40 && !focusExisting(); i++ { // the first copy may still be starting
			time.Sleep(250 * time.Millisecond)
		}
		return
	}
	defer windows.CloseHandle(mutex)

	w := webview2.NewWithOptions(webview2.WebViewOptions{
		AutoFocus: true,
		DataPath:  filepath.Join(dataDir(), "webview2"),
		WindowOptions: webview2.WindowOptions{
			Title: windowTitle, Width: 1240, Height: 880, IconId: iconResourceID, Center: true,
		},
	})
	if w == nil {
		runWithoutWebView()
		return
	}
	defer w.Destroy()
	w.SetSize(760, 560, webview2.HintMin)
	w.SetHtml(loadingPage)

	var srv *server
	started := make(chan struct{})
	go func() {
		defer close(started)
		dir, err := ensureRuntime()
		if err == nil {
			srv, err = startServer(dir, nil, hidden)
		}
		w.Dispatch(func() {
			if err != nil {
				w.SetHtml(pageHTML(errorTitle, "Подробности ниже. Лог: "+filepath.Join(dataDir(), "aihub.log"), err.Error()))
				return
			}
			w.Navigate(srv.url)
		})
	}()
	w.Run()
	<-started // window closed while starting: still shut the server down
	if srv != nil {
		srv.stop()
	}
}

// runWithoutWebView covers PCs without the WebView2 runtime: show aihub in an Edge app window
// (no tabs or address bar), or in the default browser as a last resort.
func runWithoutWebView() {
	dir, err := ensureRuntime()
	var srv *server
	if err == nil {
		srv, err = startServer(dir, nil, hidden)
	}
	if err != nil {
		messageBox(errorTitle + "\n\n" + err.Error())
		return
	}
	defer srv.stop()
	for _, base := range []string{os.Getenv("ProgramFiles(x86)"), os.Getenv("ProgramFiles")} {
		edge := filepath.Join(base, "Microsoft", "Edge", "Application", "msedge.exe")
		if _, err := os.Stat(edge); err != nil {
			continue
		}
		// A separate profile keeps this process alive exactly as long as the app window.
		cmd := exec.Command(edge, "--app="+srv.url, "--no-first-run",
			"--user-data-dir="+filepath.Join(dataDir(), "edge"))
		if cmd.Run() == nil {
			return
		}
	}
	messageBox(webviewMissingNotes)
	exec.Command("rundll32", "url.dll,FileProtocolHandler", srv.url).Start()
	srv.cmd.Wait() // the server exits on its own once the page has been closed for a while
}
