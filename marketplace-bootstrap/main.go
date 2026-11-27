package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

var payloadSHA string

func unpack(archive, destination string) error {
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	digest := sha256.New()
	_, err = io.Copy(digest, f)
	f.Close()
	if err != nil {
		return err
	}
	if hex.EncodeToString(digest.Sum(nil)) != payloadSHA {
		return fmt.Errorf("portable payload checksum mismatch")
	}
	source, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer source.Close()
	for _, entry := range source.File {
		name := filepath.FromSlash(entry.Name)
		path := filepath.Join(destination, name)
		relative, err := filepath.Rel(destination, path)
		if err != nil || filepath.IsAbs(name) || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) || entry.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("unsafe portable payload entry")
		}
		if entry.FileInfo().IsDir() {
			if err = os.MkdirAll(path, 0700); err != nil {
				return err
			}
			continue
		}
		if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return err
		}
		input, err := entry.Open()
		if err != nil {
			return err
		}
		mode := entry.Mode().Perm()
		if mode == 0 {
			mode = 0600
		}
		output, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode)
		if err != nil {
			input.Close()
			return err
		}
		_, err = io.Copy(output, input)
		input.Close()
		closeErr := output.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return os.WriteFile(filepath.Join(destination, ".payload-sha256"), []byte(payloadSHA), 0600)
}

func main() {
	exe, err := os.Executable()
	if err != nil {
		fail("Cannot locate marketplace plugin")
	}
	root := filepath.Clean(filepath.Join(filepath.Dir(exe), "..", ".."))
	home, err := os.UserHomeDir()
	if err != nil {
		fail("Cannot locate user data directory")
	}
	base := filepath.Join(home, ".local", "share", "conversation-oss-archive")
	cache := filepath.Join(base, "marketplace-payloads")
	if os.MkdirAll(cache, 0700) != nil {
		fail("Cannot create runtime cache")
	}
	destination := filepath.Join(cache, payloadSHA)
	marker, err := os.ReadFile(filepath.Join(destination, ".payload-sha256"))
	if err != nil || string(marker) != payloadSHA {
		temporary, err := os.MkdirTemp(cache, ".extract-")
		if err != nil {
			fail("Cannot prepare runtime")
		}
		defer os.RemoveAll(temporary)
		if err = unpack(filepath.Join(root, "portable.zip"), temporary); err != nil {
			fail(err.Error())
		}
		if err = os.Rename(temporary, destination); err != nil {
			marker, readErr := os.ReadFile(filepath.Join(destination, ".payload-sha256"))
			if readErr != nil || string(marker) != payloadSHA {
				fail("Cannot install portable payload")
			}
		}
	}
	platform := runtime.GOOS + "-" + runtime.GOARCH
	binary := "session-archive"
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	command := exec.Command(filepath.Join(destination, "bin", platform, binary), os.Args[1:]...)
	environment := []string{}
	for _, value := range os.Environ() {
		key := strings.ToUpper(strings.SplitN(value, "=", 2)[0])
		if key != "PLUGIN_ROOT" && key != "PLUGIN_DATA" {
			environment = append(environment, value)
		}
	}
	command.Env = append(environment, "PLUGIN_ROOT="+destination, "PLUGIN_DATA="+filepath.Join(base, "data"))
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err = command.Run(); err != nil {
		if code, ok := err.(*exec.ExitError); ok {
			os.Exit(code.ExitCode())
		}
		fail("Portable runtime could not start")
	}
}

func fail(message string) { fmt.Fprintln(os.Stderr, message); os.Exit(1) }
