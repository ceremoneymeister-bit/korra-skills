#!/bin/bash
# Basic audit: read system counters and selected metadata, never change settings.
set -u
export LC_ALL=C
[ "$(/usr/bin/uname -s)" = Darwin ] || { echo "Ожидался Mac"; exit 70; }
section() { printf '\n[%s]\n' "$1"; }
section context
/bin/date -u '+utc=%Y-%m-%dT%H:%M:%SZ'
/usr/bin/sw_vers
/usr/sbin/sysctl hw.model hw.memsize hw.ncpu hw.optional.arm64 2>/dev/null
printf 'shell_arch='; /usr/bin/uname -m
printf 'translated='; /usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || printf 'unavailable\n'
/usr/bin/uptime
section cpu_samples
# Per-process CPU is per logical core. The first top sample is not an interval.
/usr/bin/top -l 3 -s 2 -n 8 -stats pid,command,cpu,mem
section memory
/usr/sbin/sysctl vm.swapusage
/usr/bin/vm_stat
section power
/usr/bin/pmset -g batt
/usr/bin/pmset -g custom
/usr/bin/pmset -g therm
section battery_health
/usr/sbin/system_profiler -timeout 10 SPPowerDataType 2>/dev/null |
    /usr/bin/awk '/Cycle Count:|Condition:|Maximum Capacity:/'
section displays
/usr/sbin/system_profiler -timeout 10 SPDisplaysDataType 2>/dev/null |
    /usr/bin/awk '/Chipset Model:|Type:|Vendor:|Resolution:|Main Display:|Online:|Connection Type:|Display Type:/'
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" != 1 ]; then
    section intel_gpu
    /usr/sbin/ioreg -l -w0 -r -c AppleMuxControl 2>/dev/null |
        /usr/bin/awk '/"ActiveGPU"|"policy"|"GPUPowered"|"ExternalDisplayPresent"|"task-list"/'
fi
section disk
/bin/df -Pk / /System/Volumes/Data
section spotlight_current_system
/usr/bin/mdutil -s / /System/Volumes/Data
section launch_items
for folder in "$HOME/Library/LaunchAgents" /Library/LaunchAgents /Library/LaunchDaemons; do
    [ -d "$folder" ] || continue
    case "$folder" in "$HOME"/*) printf 'scope=user\n';; *) printf 'scope=system\n';; esac
    for file in "$folder"/*.plist; do
        [ -f "$file" ] || continue
        printf 'label='
        /usr/bin/plutil -extract Label raw -o - "$file" 2>/dev/null || printf 'unavailable\n'
        # No ProgramArguments, EnvironmentVariables, contents of user documents.
    done
done
section background_items
if [ -x /usr/bin/sfltool ]; then
    # Modern registrations supplement the legacy LaunchAgents directories.
    /usr/bin/sfltool dumpbtm 2>/dev/null |
        /usr/bin/awk '/^[[:space:]]*(Name|Developer Name|Type|Disposition):/'
    printf 'note=empty output is inconclusive; check Login Items in System Settings\n'
else
    printf 'status=unavailable\n'
fi
section audit_limits
printf '%s\n' \
    'No file contents, browser history, full preferences, process arguments or serial numbers collected.' \
    'Missing counters and unavailable permissions are not proof of system health.' \
    'Temperature, cooling condition and app responsiveness require a workload-specific follow-up.'
