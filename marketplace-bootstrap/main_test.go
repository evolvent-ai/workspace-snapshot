package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func payload(t *testing.T, name string, symlink bool) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "payload.zip")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	z := zip.NewWriter(f)
	header := &zip.FileHeader{Name: name}
	if symlink {
		header.SetMode(os.ModeSymlink | 0777)
	} else {
		header.SetMode(0755)
	}
	w, err := z.CreateHeader(header)
	if err != nil {
		t.Fatal(err)
	}
	w.Write([]byte("content"))
	z.Close()
	f.Close()
	data, _ := os.ReadFile(path)
	sum := sha256.Sum256(data)
	payloadSHA = hex.EncodeToString(sum[:])
	return path
}

func TestUnpackVerifiedPayload(t *testing.T) {
	path := payload(t, "bin/program", false)
	destination := t.TempDir()
	if err := unpack(path, destination); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(destination, "bin/program"))
	if err != nil || string(data) != "content" {
		t.Fatal("payload missing")
	}
}
func TestRejectTraversal(t *testing.T) {
	path := payload(t, "../escape", false)
	if unpack(path, t.TempDir()) == nil {
		t.Fatal("accepted traversal")
	}
}
func TestRejectSymlink(t *testing.T) {
	path := payload(t, "link", true)
	if unpack(path, t.TempDir()) == nil {
		t.Fatal("accepted symlink")
	}
}
func TestRejectWrongChecksum(t *testing.T) {
	path := payload(t, "file", false)
	payloadSHA = "incorrect"
	if unpack(path, t.TempDir()) == nil {
		t.Fatal("accepted wrong checksum")
	}
}
