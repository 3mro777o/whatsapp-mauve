# whatsapp-mauve — dev targets. Run from src/: cd src && make test  (paths below are relative to src/)
.PHONY: image test install import import-latest mirror enrich viewer sync-status sync-logs pair uninstall-units

image:            ## build the docker image (whatsapp-mauve:local)
	../wa build
test:             ## self-checks on temp dirs
	python3 tools/test_purge.py && python3 tools/test_import.py && python3 tools/updater.py
install:          ## deps, commands, systemd user units (wa-sync at boot)
	../legacy/setup.sh
import:           ## import ./msgstore.db (decrypted) into archive.db
	python3 tools/import_msgstore.py
import-latest:    ## decrypt + import the newest Databases/msgstore*.crypt15
	python3 tools/import_msgstore.py --latest
mirror:           ## hardlink the phone Media folder into the store
	python3 tools/import_msgstore.py --mirror-only
enrich:           ## thumbnails / waveforms for new media
	python3 tools/enrich.py
viewer:           ## start the viewer (on demand)
	tools/wa-archive
sync-status:
	tools/wa-sync/wa-sync status
sync-logs:
	tools/wa-sync/wa-sync logs
pair:             ## link this archive as a WhatsApp device (QR)
	tools/wa-sync/wa-sync pair
uninstall-units:
	../legacy/setup.sh --uninstall-units
