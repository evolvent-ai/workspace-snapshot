package main

import (
 "fmt"
 "os"
 "os/exec"
 "path/filepath"
 "runtime"
 "strings"
)

func main() {
 exe, err := os.Executable()
 if err != nil { fail("Cannot locate bundled runtime") }
 root := filepath.Clean(filepath.Join(filepath.Dir(exe), "..", ".."))
 platform := runtime.GOOS + "-" + runtime.GOARCH
 pythonHome := filepath.Join(root,"runtime",platform,"python")
 python := filepath.Join(pythonHome,"bin","python3")
 if runtime.GOOS == "windows" { python = filepath.Join(pythonHome,"python.exe") }
 if _,err = os.Stat(python); err != nil { fail("This package does not contain a runtime for "+platform) }
 args := append([]string{filepath.Join(root,"hooks","auto_archive.py")},os.Args[1:]...)
 cmd := exec.Command(python,args...)
 env := []string{}
 for _, value := range os.Environ() {
  key := strings.ToUpper(strings.SplitN(value,"=",2)[0])
  if key != "PYTHONPATH" && key != "PYTHONHOME" && key != "CONVERSATION_OSS_PORTABLE_GIT" && key != "PLUGIN_ROOT" {
   env = append(env,value)
  }
 }
 env = append(env,"PLUGIN_ROOT="+root,"PYTHONHOME="+pythonHome,
  "PYTHONPATH="+filepath.Join(root,"vendor")+string(os.PathListSeparator)+filepath.Join(root,"hooks"),
  "CONVERSATION_OSS_PORTABLE_GIT=1","PYTHONNOUSERSITE=1","PYTHONDONTWRITEBYTECODE=1")
 cmd.Env=env; cmd.Stdin=os.Stdin;cmd.Stdout=os.Stdout;cmd.Stderr=os.Stderr
 if err=cmd.Run();err!=nil {
  if value,ok:=err.(*exec.ExitError);ok { os.Exit(value.ExitCode()) }
  fail("Bundled archiver could not start")
 }
}

func fail(message string) { fmt.Fprintln(os.Stderr,message);os.Exit(1) }
