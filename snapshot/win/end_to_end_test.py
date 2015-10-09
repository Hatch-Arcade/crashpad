#!/usr/bin/env python

# Copyright 2015 The Crashpad Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import random
import re
import subprocess
import sys
import tempfile

g_temp_dirs = []


def MakeTempDir():
  global g_temp_dirs
  new_dir = tempfile.mkdtemp()
  g_temp_dirs.append(new_dir)
  return new_dir


def CleanUpTempDirs():
  global g_temp_dirs
  for d in g_temp_dirs:
    subprocess.call(['rmdir', '/s', '/q', d], shell=True)


def FindInstalledWindowsApplication(app_path):
  search_paths = [os.getenv('PROGRAMFILES(X86)'),
                  os.getenv('PROGRAMFILES'),
                  os.getenv('LOCALAPPDATA')]
  search_paths += os.getenv('PATH', '').split(os.pathsep)

  for search_path in search_paths:
    if not search_path:
      continue
    path = os.path.join(search_path, app_path)
    if os.path.isfile(path):
      return path

  return None


def GetCdbPath():
  """Search in some reasonable places to find cdb.exe. Searches x64 before x86
  and newer versions before older versions.
  """
  possible_paths = (
      os.path.join('Windows Kits', '10', 'Debuggers', 'x64'),
      os.path.join('Windows Kits', '10', 'Debuggers', 'x86'),
      os.path.join('Windows Kits', '8.1', 'Debuggers', 'x64'),
      os.path.join('Windows Kits', '8.1', 'Debuggers', 'x86'),
      os.path.join('Windows Kits', '8.0', 'Debuggers', 'x64'),
      os.path.join('Windows Kits', '8.0', 'Debuggers', 'x86'),
      'Debugging Tools For Windows (x64)',
      'Debugging Tools For Windows (x86)',
      'Debugging Tools For Windows',)
  for possible_path in possible_paths:
    app_path = os.path.join(possible_path, 'cdb.exe')
    app_path = FindInstalledWindowsApplication(app_path)
    if app_path:
      return app_path
  return None


def GetDumpFromCrashyProgram(out_dir, pipe_name):
  """Initialize a crash database, run crashpad_handler, run crashy_program
  connecting to the crash_handler. Returns the minidump generated by
  crash_handler for further testing.
  """
  test_database = MakeTempDir()
  handler = None

  try:
    if subprocess.call(
        [os.path.join(out_dir, 'crashpad_database_util.exe'), '--create',
         '--database=' + test_database]) != 0:
      print 'could not initialize report database'
      return None

    handler = subprocess.Popen([
        os.path.join(out_dir, 'crashpad_handler.exe'),
        '--pipe-name=' + pipe_name,
        '--database=' + test_database
    ])

    subprocess.call([os.path.join(out_dir, 'crashy_program.exe'), pipe_name])

    out = subprocess.check_output([
        os.path.join(out_dir, 'crashpad_database_util.exe'),
        '--database=' + test_database,
        '--show-completed-reports',
        '--show-all-report-info',
    ])
    for line in out.splitlines():
      if line.strip().startswith('Path:'):
        return line.partition(':')[2].strip()
  finally:
    if handler:
      handler.kill()


class CdbRun(object):
  """Run cdb.exe passing it a cdb command and capturing the output.
  `Check()` searches for regex patterns in sequence allowing verification of
  expected output.
  """

  def __init__(self, cdb_path, dump_path, command):
    # Run a command line that loads the dump, runs the specified cdb command,
    # and then quits, and capturing stdout.
    self.out = subprocess.check_output([
        cdb_path,
        '-z', dump_path,
        '-c', command + ';q'
    ])

  def Check(self, pattern, message):
    match_obj = re.search(pattern, self.out)
    if match_obj:
      # Matched. Consume up to end of match.
      self.out = self.out[match_obj.end(0):]
      print 'ok - %s' % message
    else:
      print >>sys.stderr, '-' * 80
      print >>sys.stderr, 'FAILED - %s' % message
      print >>sys.stderr, '-' * 80
      print >>sys.stderr, 'did not match:\n  %s' % pattern
      print >>sys.stderr, '-' * 80
      print >>sys.stderr, 'remaining output was:\n  %s' % self.out
      print >>sys.stderr, '-' * 80
      sys.exit(1)


def RunTests(cdb_path, dump_path, pipe_name):
  """Runs various tests in sequence. Runs a new cdb instance on the dump for
  each block of tests to reduce the chances that output from one command is
  confused for output from another.
  """
  out = CdbRun(cdb_path, dump_path, '.ecxr')
  out.Check('This dump file has an exception of interest stored in it',
            'captured exception')
  out.Check(
      'crashy_program!crashpad::`anonymous namespace\'::SomeCrashyFunction',
      'exception at correct location')

  out = CdbRun(cdb_path, dump_path, '!peb')
  out.Check(r'PEB at', 'found the PEB')
  out.Check(r'Ldr\.InMemoryOrderModuleList:.*\d+ \. \d+', 'PEB_LDR_DATA saved')
  out.Check(r'Base TimeStamp                     Module', 'module list present')
  pipe_name_escaped = pipe_name.replace('\\', '\\\\')
  out.Check(r'CommandLine: *\'.*crashy_program.exe *' + pipe_name_escaped,
            'some PEB data is correct')
  out.Check(r'SystemRoot=C:\\Windows', 'some of environment captured')

  out = CdbRun(cdb_path, dump_path, '!teb')
  out.Check(r'TEB at', 'found the TEB')
  out.Check(r'ExceptionList:\s+[0-9a-fA-F]+', 'some valid teb data')
  out.Check(r'LastErrorValue:\s+2', 'correct LastErrorValue')

  out = CdbRun(cdb_path, dump_path, '!gle')
  out.Check('LastErrorValue: \(Win32\) 0x2 \(2\) - The system cannot find the '
            'file specified.', '!gle gets last error')
  out.Check('LastStatusValue: \(NTSTATUS\) 0xc000000f - {File Not Found}  The '
            'file %hs does not exist.', '!gle gets last ntstatus')

  # Locks.
  if False:  # The code for these isn't landed yet.
    out = CdbRun(cdb_path, dump_path, '!locks')
    out.Check(r'CritSec crashy_program!crashpad::`anonymous namespace\'::'
              r'g_test_critical_section', 'lock was captured')
    out.Check(r'\*\*\* Locked', 'lock debug info was captured, and is locked')


def main(args):
  try:
    if len(args) != 1:
      print >>sys.stderr, 'must supply out dir'
      return 1

    cdb_path = GetCdbPath()
    if not cdb_path:
      print >>sys.stderr, 'could not find cdb'
      return 1

    pipe_name = r'\\.\pipe\end-to-end_%s_%s' % (
        os.getpid(), str(random.getrandbits(64)))

    dump_path = GetDumpFromCrashyProgram(args[0], pipe_name)
    if not dump_path:
      return 1

    RunTests(cdb_path, dump_path, pipe_name)

    return 0
  finally:
    CleanUpTempDirs()


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
