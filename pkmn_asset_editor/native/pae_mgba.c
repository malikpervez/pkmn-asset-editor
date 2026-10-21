/*
 * pae_mgba: a tiny headless mGBA driver for play-testing builds.
 *
 * Reads one command per line on stdin and answers one line on stdout:
 *   frames N KEYS       run N frames holding the key mask KEYS (GBA KEYINPUT bit order)
 *   r8|r16|r32 ADDR     read memory through the bus            -> "ok VALUE"
 *   w8|w16|w32 ADDR V   write memory through the bus
 *   shot PATH           write the current frame as raw RGBA (240x160x4)
 *   save PATH | load PATH   savestate to/from a file
 *   reset               power-cycle (keeps the save data)
 *   call ADDR [R0..R3]  run a game function (thumb address) to completion -> "ok R0"
 *   audio RATE          start collecting sound at RATE samples per second (stereo, 16-bit)
 *   live N KEYS PATH    like frames, then write the picture and the sound since the last live to PATH:
 *                       u32 sample count, 240x160 RGBA, then the samples (left/right interleaved)
 *   quit
 * Numbers may be decimal or 0x-prefixed hex.
 *
 * Usage: pae_mgba ROM [SAVEFILE]   SAVEFILE is the cartridge save (.sav), created if missing and written
 *        by the game itself when it saves (flushed a few frames after each write).
 * Build: cc -O2 -o pae_mgba pae_mgba.c -lmgba   (Debian/Ubuntu: apt install libmgba-dev)
 */
#include <mgba/core/blip_buf.h>
#include <mgba/core/core.h>
#include <mgba/core/config.h>
#include <mgba/core/log.h>
#include <mgba/core/serialize.h>
#include <mgba/internal/arm/arm.h>
#include <mgba/internal/arm/isa-inlines.h>
#include <mgba-util/vfs.h>

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned width, height;

static void quietLog(struct mLogger* logger, int category, enum mLogLevel level, const char* format, va_list args) {
	(void) logger;
	(void) category;
	if (level & mLOG_FATAL) {
		vfprintf(stderr, format, args);
		fputc('\n', stderr);
	}
}

static struct mLogger quietLogger = { .log = quietLog };
static color_t* video;
static int audioOn;
static short audio[2 * 8192];

static void writeVideo(FILE* f);

static int writeShot(const char* path) {
	FILE* f = fopen(path, "wb");
	if (!f) {
		return 0;
	}
	writeVideo(f);
	fclose(f);
	return 1;
}

/* The picture and the sound collected since the last call, for live play. */
static int writeLive(struct mCore* core, const char* path) {
	FILE* f = fopen(path, "wb");
	if (!f) {
		return 0;
	}
	uint32_t n = 0;
	if (audioOn) {
		struct blip_t* left = core->getAudioChannel(core, 0);
		struct blip_t* right = core->getAudioChannel(core, 1);
		int avail = blip_samples_avail(left);
		if (avail > 8192) {
			avail = 8192;
		}
		blip_read_samples(left, audio, avail, 1);
		blip_read_samples(right, audio + 1, avail, 1);
		n = (uint32_t) avail;
	}
	fwrite(&n, 4, 1, f);
	writeVideo(f);
	fwrite(audio, 4, n, f);
	fclose(f);
	return 1;
}

static void writeVideo(FILE* f) {
	unsigned char* row = malloc(width * 4);
	for (unsigned y = 0; y < height; ++y) {
		for (unsigned x = 0; x < width; ++x) {
			color_t c = video[y * width + x];
#ifdef COLOR_16_BIT
			unsigned r = (c & 0x1F) << 3, g = ((c >> 5) & 0x1F) << 3, b = ((c >> 10) & 0x1F) << 3;
#else
			unsigned r = c & 0xFF, g = (c >> 8) & 0xFF, b = (c >> 16) & 0xFF;
#endif
			row[x * 4] = r;
			row[x * 4 + 1] = g;
			row[x * 4 + 2] = b;
			row[x * 4 + 3] = 255;
		}
		fwrite(row, 1, width * 4, f);
	}
	free(row);
}

/* Call a function inside the running game, the way a debugger would: save the CPU state, point PC at
 * the function with arguments in r0-r3 and a sentinel return address in LR, single-step until it
 * returns there, then restore the CPU so the game carries on exactly where it was. Interrupts stay
 * as they were, so code that waits on timers or VBlank (e.g. flash saving) still works. */
#define CALL_SENTINEL 0x08000000u
static int callFunction(struct mCore* core, uint32_t addr, const uint32_t* args, int nargs, uint32_t* result) {
	struct ARMCore* cpu = core->cpu;
	struct ARMRegisterFile saved = cpu->regs;
	uint32_t savedPrefetch[2] = { cpu->prefetch[0], cpu->prefetch[1] };
	enum ExecutionMode savedMode = cpu->executionMode;
	int savedHalted = cpu->halted;
	for (int i = 0; i < nargs && i < 4; ++i) {
		cpu->gprs[i] = args[i];
	}
	cpu->gprs[ARM_SP] -= 0x200; /* stay clear of whatever the interrupted code keeps on its stack */
	cpu->gprs[ARM_LR] = CALL_SENTINEL | 1;
	cpu->halted = 0;
	_ARMSetMode(cpu, MODE_THUMB);
	cpu->gprs[ARM_PC] = addr & ~1u;
	ThumbWritePC(cpu);
	long steps = 0;
	int ok = 0;
	while (steps++ < 400000000L) {
		uint32_t pc = (uint32_t) cpu->gprs[ARM_PC] - (cpu->executionMode == MODE_THUMB ? 4 : 8);
		if ((pc & ~1u) == CALL_SENTINEL) {
			ok = 1;
			break;
		}
		core->step(core);
	}
	*result = (uint32_t) cpu->gprs[0];
	cpu->regs = saved;
	_ARMSetMode(cpu, savedMode);
	cpu->memory.setActiveRegion(cpu, cpu->gprs[ARM_PC]);
	cpu->prefetch[0] = savedPrefetch[0];
	cpu->prefetch[1] = savedPrefetch[1];
	cpu->halted = savedHalted;
	return ok;
}

// The game's clock (games with a day/night tint read it): PAE_RTC is the Unix time it starts from at power-on, so a
// replay sees the same time of day on every computer; the emulated clock then runs with the game. Unset: the real
// time. Applied again after loading a state, which would otherwise bring back the clock it was saved with.
static void applyClock(struct mCore* core) {
	const char* rtcStart = getenv("PAE_RTC");
	if (rtcStart && *rtcStart) {
		core->rtc.override = RTC_FAKE_EPOCH;
		core->rtc.value = strtoll(rtcStart, NULL, 10) * 1000LL;
	}
}

int main(int argc, char** argv) {
	if (argc < 2) {
		fprintf(stderr, "usage: %s ROM\n", argv[0]);
		return 2;
	}
	mLogSetDefaultLogger(&quietLogger);
	struct mCore* core = mCoreFind(argv[1]);
	if (!core || !core->init(core)) {
		fprintf(stderr, "cannot open %s\n", argv[1]);
		return 1;
	}
	mCoreInitConfig(core, NULL);
	core->desiredVideoDimensions(core, &width, &height);
	video = calloc(width * height, sizeof(color_t));
	core->setVideoBuffer(core, video, width);
	if (!mCoreLoadFile(core, argv[1])) {
		fprintf(stderr, "cannot load %s\n", argv[1]);
		return 1;
	}
	if (argc > 2) {
		struct VFile* save = VFileOpen(argv[2], O_CREAT | O_RDWR);
		if (!save || !core->loadSave(core, save)) {
			fprintf(stderr, "cannot use save file %s\n", argv[2]);
			return 1;
		}
	}
	applyClock(core);
	core->reset(core);
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("ready %u %u\n", width, height);

	char line[4096], cmd[16], arg[4000];
	while (fgets(line, sizeof(line), stdin)) {
		unsigned long a = 0, b = 0;
		arg[0] = 0;
		if (sscanf(line, "%15s", cmd) != 1) {
			continue;
		}
		if (!strcmp(cmd, "frames")) {
			if (sscanf(line, "%*s %li %li", (long*) &a, (long*) &b) < 1) {
				puts("err usage: frames N KEYS");
				continue;
			}
			core->setKeys(core, (uint32_t) b);
			for (unsigned long i = 0; i < a; ++i) {
				core->runFrame(core);
			}
			puts("ok");
		} else if (!strcmp(cmd, "r8") || !strcmp(cmd, "r16") || !strcmp(cmd, "r32")) {
			sscanf(line, "%*s %li", (long*) &a);
			uint32_t v = cmd[1] == '8' ? core->busRead8(core, a) : cmd[1] == '1' ? core->busRead16(core, a) : core->busRead32(core, a);
			printf("ok %u\n", v);
		} else if (!strcmp(cmd, "w8") || !strcmp(cmd, "w16") || !strcmp(cmd, "w32")) {
			sscanf(line, "%*s %li %li", (long*) &a, (long*) &b);
			if (cmd[1] == '8') {
				core->busWrite8(core, a, b);
			} else if (cmd[1] == '1') {
				core->busWrite16(core, a, b);
			} else {
				core->busWrite32(core, a, b);
			}
			puts("ok");
		} else if (!strcmp(cmd, "shot") && sscanf(line, "%*s %3999[^\n]", arg) == 1) {
			puts(writeShot(arg) ? "ok" : "err cannot write");
		} else if ((!strcmp(cmd, "save") || !strcmp(cmd, "load")) && sscanf(line, "%*s %3999[^\n]", arg) == 1) {
			int saving = cmd[0] == 's';
			struct VFile* vf = VFileOpen(arg, saving ? O_CREAT | O_TRUNC | O_RDWR : O_RDONLY);
			if (!vf) {
				puts("err cannot open state file");
				continue;
			}
			int okState = saving ? mCoreSaveStateNamed(core, vf, SAVESTATE_SAVEDATA | SAVESTATE_RTC)
			                     : mCoreLoadStateNamed(core, vf, SAVESTATE_SAVEDATA | SAVESTATE_RTC);
			vf->close(vf);
			if (!saving) {
				applyClock(core);
			}
			puts(okState ? "ok" : "err state failed");
		} else if (!strcmp(cmd, "call")) {
			uint32_t args[4] = { 0 };
			unsigned long v[5] = { 0 };
			int n = sscanf(line, "%*s %li %li %li %li %li", (long*) &v[0], (long*) &v[1], (long*) &v[2], (long*) &v[3], (long*) &v[4]);
			if (n < 1) {
				puts("err usage: call ADDR [ARGS]");
				continue;
			}
			for (int i = 1; i < n; ++i) {
				args[i - 1] = v[i];
			}
			uint32_t r0 = 0;
			if (callFunction(core, v[0], args, n - 1, &r0)) {
				printf("ok %u\n", r0);
			} else {
				puts("err function did not return");
			}
		} else if (!strcmp(cmd, "audio")) {
			if (sscanf(line, "%*s %li", (long*) &a) != 1 || a < 8000) {
				puts("err usage: audio RATE");
				continue;
			}
			core->setAudioBufferSize(core, 4096);
			for (int ch = 0; ch < 2; ++ch) {
				struct blip_t* b = core->getAudioChannel(core, ch);
				blip_set_rates(b, core->frequency(core), (double) a);
				blip_clear(b);
			}
			audioOn = 1;
			puts("ok");
		} else if (!strcmp(cmd, "live")) {
			if (sscanf(line, "%*s %li %li %3999[^\n]", (long*) &a, (long*) &b, arg) != 3) {
				puts("err usage: live N KEYS PATH");
				continue;
			}
			core->setKeys(core, (uint32_t) b);
			for (unsigned long i = 0; i < a; ++i) {
				core->runFrame(core);
			}
			puts(writeLive(core, arg) ? "ok" : "err cannot write");
		} else if (!strcmp(cmd, "reset")) {
			core->reset(core);
			puts("ok");
		} else if (!strcmp(cmd, "quit")) {
			puts("ok");
			break;
		} else {
			puts("err unknown command");
		}
	}
	core->deinit(core);
	return 0;
}
