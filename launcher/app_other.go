//go:build !windows

// Non-Windows builds exist for development and tests: start the server and open the browser.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"syscall"
)

func main() {
	dir, err := ensureRuntime()
	if err != nil {
		fmt.Println("unpack failed:", err)
		os.Exit(1)
	}
	srv, err := startServer(dir, os.Args[1:], nil)
	if err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
	fmt.Println("aihub:", srv.url)
	if os.Getenv("AIHUB_NO_BROWSER") == "" {
		opener := "xdg-open"
		if runtime.GOOS == "darwin" {
			opener = "open"
		}
		exec.Command(opener, srv.url).Start()
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	srv.stop()
}
