@echo off
cd /d "%~dp0\.."
py -m peer.peer demo_workspace\torrents\demo_single.torrent.json demo_workspace\torrents\demo_multi.torrent.json --download-root demo_workspace\leecher_storage --log-dir demo_workspace\logs\leecher --port-start 6891 --exit-on-complete
pause
