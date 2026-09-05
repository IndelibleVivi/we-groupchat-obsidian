/*
 * find_all_keys_macos.c - macOS WeChat memory key scanner
 *
 * Scans WeChat process memory for SQLCipher encryption keys in both the
 * legacy x'<key_hex><salt_hex>' / x'<key_hex>' forms and the protected
 * binary cipher context used by WeChat 4.1.11.
 *
 * Prerequisites:
 *   - WeChat must be ad-hoc signed (or SIP disabled)
 *   - The target must permit task inspection; the Python wrapper retries with
 *     sudo only when the ordinary scan is denied
 *
 * Build:
 *   cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
 *
 * Usage:
 *   ./find_all_keys_macos [pid]
 *   If pid is omitted, automatically finds WeChat PID.
 *
 * Output: JSON file at ./all_keys.json (compatible with decrypt_db.py)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <ftw.h>
#include <limits.h>
#include <pwd.h>
#include <stdint.h>
#include <sys/stat.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>

#define MAX_KEYS 256
#define KEY_SIZE 32
#define SALT_SIZE 16
#define KEY_HEX_LEN 64
#define SALT_HEX_LEN 32
#define COMBINED_HEX_LEN (KEY_HEX_LEN + SALT_HEX_LEN)
#define MAX_PATTERN_BYTES (COMBINED_HEX_LEN + 3)
#define CODEC_CTX_SIZE 136
#define SCAN_OVERLAP CODEC_CTX_SIZE
#define CHUNK_SIZE (2 * 1024 * 1024)

/* Current WeChat builds keep SQLCipher pass/key buffers XOR-protected while
 * idle. Python supplies the exact build-profile mask as the final argument;
 * the scanner never guesses it. Page-one HMAC verification remains the
 * authority for every decoded candidate. */
static unsigned char g_memory_mask[KEY_SIZE];
static int g_memory_mask_enabled = 0;

typedef struct {
    char key_hex[65];
    char salt_hex[33];
    char full_pragma[100];
} key_entry_t;

/* Forward declaration */
static int read_db_salt(const char *path, char *salt_hex_out);

static int read_remote(mach_port_t task, mach_vm_address_t address,
                       void *buffer, mach_vm_size_t size) {
    mach_vm_size_t read_size = 0;
    kern_return_t kr = mach_vm_read_overwrite(
        task, address, size, (mach_vm_address_t)buffer, &read_size);
    return kr == KERN_SUCCESS && read_size == size ? 0 : -1;
}

static int read_i32(const unsigned char *buffer, size_t offset) {
    int value;
    memcpy(&value, buffer + offset, sizeof(value));
    return value;
}

static uint64_t read_u64(const unsigned char *buffer, size_t offset) {
    uint64_t value;
    memcpy(&value, buffer + offset, sizeof(value));
    return value;
}

static void bytes_to_hex(const unsigned char *bytes, size_t size, char *out) {
    for (size_t i = 0; i < size; i++)
        sprintf(out + i * 2, "%02x", bytes[i]);
    out[size * 2] = '\0';
}

static int add_key(key_entry_t *keys, int *key_count,
                   const unsigned char key[KEY_SIZE], const char *salt_hex) {
    char key_hex[KEY_HEX_LEN + 1];
    bytes_to_hex(key, KEY_SIZE, key_hex);
    const char *salt = salt_hex ? salt_hex : "";

    for (int i = 0; i < *key_count; i++) {
        if (strcmp(keys[i].key_hex, key_hex) == 0 &&
            strcmp(keys[i].salt_hex, salt) == 0)
            return 0;
    }
    if (*key_count >= MAX_KEYS)
        return -1;

    key_entry_t *entry = &keys[*key_count];
    strcpy(entry->key_hex, key_hex);
    strcpy(entry->salt_hex, salt);
    snprintf(entry->full_pragma, sizeof(entry->full_pragma),
             "x'%s%s'", key_hex, salt);
    (*key_count)++;
    return 1;
}

static int looks_like_wechat_4_1_11_codec(const unsigned char *ctx) {
    return read_i32(ctx, 4) == 256000 &&
           read_i32(ctx, 8) == 2 &&
           read_i32(ctx, 12) == 16 &&
           read_i32(ctx, 16) == 32 &&
           read_i32(ctx, 20) == 16 &&
           read_i32(ctx, 24) == 16 &&
           read_i32(ctx, 28) == 4096 &&
           read_i32(ctx, 32) == 99 &&
           read_i32(ctx, 36) == 80 &&
           read_i32(ctx, 40) == 64 &&
           read_i32(ctx, 44) == 0 &&
           read_i32(ctx, 48) == 2 &&
           read_i32(ctx, 52) == 2 &&
           read_i32(ctx, 64) == 3;
}

static void collect_codec_keys(mach_port_t task, const unsigned char *ctx,
                               key_entry_t *keys, int *key_count) {
    unsigned char salt[SALT_SIZE];
    char salt_hex[SALT_HEX_LEN + 1];
    salt_hex[0] = '\0';
    uint64_t salt_ptr = read_u64(ctx, 72);
    if (salt_ptr && read_remote(task, salt_ptr, salt, sizeof(salt)) == 0)
        bytes_to_hex(salt, sizeof(salt), salt_hex);

    const size_t cipher_offsets[] = {104, 112};
    for (size_t index = 0;
         index < sizeof(cipher_offsets) / sizeof(cipher_offsets[0]); index++) {
        uint64_t cipher_ptr = read_u64(ctx, cipher_offsets[index]);
        unsigned char cipher_ctx[40];
        if (!cipher_ptr ||
            read_remote(task, cipher_ptr, cipher_ctx, sizeof(cipher_ctx)) != 0)
            continue;
        uint64_t key_ptr = read_u64(cipher_ctx, 8);
        unsigned char stored_key[KEY_SIZE];
        unsigned char decoded_key[KEY_SIZE];
        if (!key_ptr ||
            read_remote(task, key_ptr, stored_key, sizeof(stored_key)) != 0)
            continue;
        for (size_t i = 0; i < KEY_SIZE; i++)
            decoded_key[i] = stored_key[i] ^ g_memory_mask[i];

        /* Usually decoded_key is the useful candidate. Keep stored_key too:
         * a concurrent codec operation may have temporarily unmasked it. */
        add_key(keys, key_count, decoded_key, salt_hex);
        add_key(keys, key_count, stored_key, salt_hex);
    }
}

/* nftw callback state for collecting DB files */
#define MAX_DBS 256
static char g_db_salts[MAX_DBS][33];
static char g_db_names[MAX_DBS][256];
static int g_db_count = 0;
static char g_db_root[1024] = {0};

static int path_has_db_storage(const char *fpath) {
    return strstr(fpath, "/db_storage/") != NULL || strstr(fpath, "db_storage/") == fpath;
}

static int nftw_collect_db(const char *fpath, const struct stat *sb,
                           int typeflag, struct FTW *ftwbuf) {
    (void)sb; (void)ftwbuf;
    if (typeflag != FTW_F) return 0;
    if (!path_has_db_storage(fpath)) return 0;
    size_t len = strlen(fpath);
    if (len < 3 || strcmp(fpath + len - 3, ".db") != 0) return 0;
    if (g_db_count >= MAX_DBS) return 0;

    char salt[33];
    if (read_db_salt(fpath, salt) != 0) return 0;

    strcpy(g_db_salts[g_db_count], salt);
    /* Extract relative path from db_storage/ */
    const char *rel = NULL;
    if (g_db_root[0] != '\0' && strncmp(fpath, g_db_root, strlen(g_db_root)) == 0) {
        rel = fpath + strlen(g_db_root);
        if (*rel == '/') rel++;
    }
    if (!rel || !rel[0]) {
        rel = strstr(fpath, "db_storage/");
        if (rel) rel += strlen("db_storage/");
    }
    if (!rel || !rel[0]) {
        rel = strrchr(fpath, '/');
        rel = rel ? rel + 1 : fpath;
    }
    strncpy(g_db_names[g_db_count], rel, 255);
    g_db_names[g_db_count][255] = '\0';
    printf("  %s: salt=%s\n", g_db_names[g_db_count], salt);
    g_db_count++;
    return 0;
}

static int is_hex_char(unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static int parse_memory_mask(const char *hex) {
    if (!hex || strlen(hex) != KEY_HEX_LEN)
        return -1;
    for (int i = 0; i < KEY_SIZE; i++) {
        unsigned int value;
        if (!is_hex_char((unsigned char)hex[i * 2]) ||
            !is_hex_char((unsigned char)hex[i * 2 + 1]) ||
            sscanf(hex + i * 2, "%2x", &value) != 1)
            return -1;
        g_memory_mask[i] = (unsigned char)value;
    }
    g_memory_mask_enabled = 1;
    return 0;
}

static pid_t run_pgrep_first(const char *cmd) {
    FILE *fp = popen(cmd, "r");
    if (!fp) return -1;
    char buf[64];
    pid_t pid = -1;
    if (fgets(buf, sizeof(buf), fp))
        pid = atoi(buf);
    pclose(fp);
    return pid;
}

static pid_t find_wechat_pid(void) {
    const char *commands[] = {
        "pgrep -x WeChat",
        "pgrep -x WeChatAppEx",
        "pgrep -x 微信",
        "pgrep -f '/WeChat\\.app/Contents/MacOS/WeChat($| )'",
        "pgrep -f '/WeChatAppEx\\.app/Contents/MacOS/WeChatAppEx($| )'",
        NULL,
    };

    for (int i = 0; commands[i] != NULL; i++) {
        pid_t pid = run_pgrep_first(commands[i]);
        if (pid > 0)
            return pid;
    }

    return -1;
}

/* Read DB salt (first 16 bytes) and return hex string */
static int read_db_salt(const char *path, char *salt_hex_out) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    unsigned char header[16];
    if (fread(header, 1, 16, f) != 16) { fclose(f); return -1; }
    fclose(f);
    /* Check if unencrypted */
    if (memcmp(header, "SQLite format 3", 15) == 0) return -1;
    for (int i = 0; i < 16; i++)
        sprintf(salt_hex_out + i * 2, "%02x", header[i]);
    salt_hex_out[32] = '\0';
    return 0;
}

int main(int argc, char *argv[]) {
    pid_t pid;
    const char *override_home = NULL;
    const char *override_db_root = NULL;
    if (argc >= 2)
        pid = atoi(argv[1]);
    else
        pid = find_wechat_pid();
    if (argc >= 3 && argv[2][0] != '\0')
        override_home = argv[2];
    if (argc >= 4 && argv[3][0] != '\0')
        override_db_root = argv[3];
    if (argc >= 5 && argv[4][0] != '\0' &&
        parse_memory_mask(argv[4]) != 0) {
        fprintf(stderr, "Invalid protected-key memory mask\n");
        return 1;
    }

    if (pid <= 0) {
        fprintf(stderr, "WeChat not running or invalid PID\n");
        return 1;
    }

    printf("============================================================\n");
    printf("  macOS WeChat Memory Key Scanner (C version)\n");
    printf("============================================================\n");
    printf("WeChat PID: %d\n", pid);

    /* Get task port */
    mach_port_t task;
    kern_return_t kr = task_for_pid(mach_task_self(), pid, &task);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "task_for_pid failed: %d\n", kr);
        fprintf(stderr, "Make sure: (1) running as root, (2) WeChat is ad-hoc signed\n");
        return 1;
    }
    printf("Got task port: %u\n", task);

    /* Resolve real user's HOME (sudo may change HOME to /var/root) */
    const char *home = override_home;
    if (!home || !home[0]) {
        home = getenv("HOME");
        const char *sudo_user = getenv("SUDO_USER");
        if (sudo_user) {
            struct passwd *pw = getpwnam(sudo_user);
            if (pw && pw->pw_dir)
                home = pw->pw_dir;
        }
    }
    if (!home) home = "/root";
    printf("User home: %s\n", home);

    /* Collect DB salts by recursively walking db_storage directories.
     * Note: POSIX glob() does not support ** recursive matching on macOS,
     * so we use nftw() to walk the directory tree instead. */
    printf("\nScanning for DB files...\n");
    if (override_db_root && override_db_root[0]) {
        char resolved[PATH_MAX];
        const char *db_root = realpath(override_db_root, resolved);
        if (!db_root) db_root = override_db_root;
        strncpy(g_db_root, db_root, sizeof(g_db_root) - 1);
        g_db_root[sizeof(g_db_root) - 1] = '\0';

        struct stat st;
        if (stat(db_root, &st) == 0 && S_ISDIR(st.st_mode)) {
            printf("  Searching configured db_dir: %s\n", db_root);
            nftw(db_root, nftw_collect_db, 20, FTW_PHYS);
        } else {
            printf("  Configured db_dir missing: %s\n", db_root);
        }
    }

    char search_roots[3][512];
    snprintf(search_roots[0], sizeof(search_roots[0]),
        "%s/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
        home);
    snprintf(search_roots[1], sizeof(search_roots[1]),
        "%s/Library/Containers/com.tencent.xinWeChat/Data/Documents",
        home);
    snprintf(search_roots[2], sizeof(search_roots[2]),
        "%s/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support",
        home);

    if (g_db_count == 0) {
        for (int i = 0; i < 3; i++) {
            struct stat st;
            if (stat(search_roots[i], &st) == 0 && S_ISDIR(st.st_mode)) {
                printf("  Searching: %s\n", search_roots[i]);
                nftw(search_roots[i], nftw_collect_db, 20, FTW_PHYS);
            }
        }
    }
    printf("Found %d encrypted DBs\n", g_db_count);

    /* Scan memory for x' patterns */
    printf("\nScanning memory for keys...\n");
    key_entry_t keys[MAX_KEYS];
    int key_count = 0;
    size_t total_scanned = 0;
    int region_count = 0;

    mach_vm_address_t addr = 0;
    while (1) {
        mach_vm_size_t size = 0;
        vm_region_basic_info_data_64_t info;
        mach_msg_type_number_t info_count = VM_REGION_BASIC_INFO_COUNT_64;
        mach_port_t obj_name;

        kr = mach_vm_region(task, &addr, &size, VM_REGION_BASIC_INFO_64,
                           (vm_region_info_t)&info, &info_count, &obj_name);
        if (kr != KERN_SUCCESS) break;
        if (size == 0) { addr++; continue; }  /* guard against infinite loop */

        if ((info.protection & (VM_PROT_READ | VM_PROT_WRITE)) ==
            (VM_PROT_READ | VM_PROT_WRITE)) {
            region_count++;

            mach_vm_address_t ca = addr;
            while (ca < addr + size) {
                mach_vm_size_t cs = addr + size - ca;
                if (cs > CHUNK_SIZE) cs = CHUNK_SIZE;

                vm_offset_t data;
                mach_msg_type_number_t dc;
                kr = mach_vm_read(task, ca, cs, &data, &dc);
                if (kr == KERN_SUCCESS) {
                    unsigned char *buf = (unsigned char *)data;
                    total_scanned += dc;

                    for (size_t i = 0; i + KEY_HEX_LEN + 3 <= dc; i++) {
                        if (buf[i] == 'x' && buf[i + 1] == '\'') {
                            int hex_len = 0;
                            if (i + COMBINED_HEX_LEN + 3 <= dc) {
                                int combined_valid = 1;
                                for (int j = 0; j < COMBINED_HEX_LEN; j++) {
                                    if (!is_hex_char(buf[i + 2 + j])) {
                                        combined_valid = 0;
                                        break;
                                    }
                                }
                                if (combined_valid &&
                                    buf[i + 2 + COMBINED_HEX_LEN] == '\'') {
                                    hex_len = COMBINED_HEX_LEN;
                                }
                            }
                            if (hex_len == 0) {
                                int key_valid = 1;
                                for (int j = 0; j < KEY_HEX_LEN; j++) {
                                    if (!is_hex_char(buf[i + 2 + j])) {
                                        key_valid = 0;
                                        break;
                                    }
                                }
                                if (key_valid && buf[i + 2 + KEY_HEX_LEN] == '\'') {
                                    hex_len = KEY_HEX_LEN;
                                }
                            }
                            if (hex_len == 0) continue;

                            char key_hex[65], salt_hex[33];
                            memcpy(key_hex, buf + i + 2, KEY_HEX_LEN);
                            key_hex[KEY_HEX_LEN] = '\0';
                            salt_hex[0] = '\0';
                            if (hex_len == COMBINED_HEX_LEN) {
                                memcpy(salt_hex, buf + i + 2 + KEY_HEX_LEN,
                                       SALT_HEX_LEN);
                                salt_hex[SALT_HEX_LEN] = '\0';
                            }

                            /* Convert to lowercase for comparison */
                            for (int j = 0; key_hex[j]; j++)
                                if (key_hex[j] >= 'A' && key_hex[j] <= 'F')
                                    key_hex[j] += 32;
                            for (int j = 0; salt_hex[j]; j++)
                                if (salt_hex[j] >= 'A' && salt_hex[j] <= 'F')
                                    salt_hex[j] += 32;

                            /* Deduplicate */
                            int dup = 0;
                            for (int k = 0; k < key_count; k++) {
                                if (strcmp(keys[k].key_hex, key_hex) == 0 &&
                                    strcmp(keys[k].salt_hex, salt_hex) == 0) {
                                    dup = 1; break;
                                }
                            }
                            if (dup) continue;

                            if (key_count < MAX_KEYS) {
                                strcpy(keys[key_count].key_hex, key_hex);
                                strcpy(keys[key_count].salt_hex, salt_hex);
                                snprintf(keys[key_count].full_pragma,
                                    sizeof(keys[key_count].full_pragma),
                                    "x'%s%s'", key_hex, salt_hex);
                                key_count++;
                            }
                        }
                    }

                    /* Supported current builds protect the persistent binary
                     * key while idle. Decode only when Python supplied an
                     * exact build-profile mask. */
                    if (g_memory_mask_enabled) {
                        for (size_t i = 0; i + CODEC_CTX_SIZE <= dc; i++) {
                            if (looks_like_wechat_4_1_11_codec(buf + i))
                                collect_codec_keys(
                                    task, buf + i, keys, &key_count);
                        }
                    }
                    mach_vm_deallocate(mach_task_self(), data, dc);
                }
                /* Advance with overlap for the longest supported literal. */
                if (cs > SCAN_OVERLAP)
                    ca += cs - SCAN_OVERLAP;
                else
                    ca += cs;
            }
        }
        addr += size;
    }

    printf("\nScan complete: %zuMB scanned, %d regions, %d unique keys\n",
           total_scanned / 1024 / 1024, region_count, key_count);

    int matched = 0;
    for (int i = 0; i < key_count; i++) {
        for (int j = 0; j < g_db_count; j++) {
            if (strcmp(keys[i].salt_hex, g_db_salts[j]) == 0) {
                matched++;
                break;
            }
        }
        /* Machine-readable lines are captured in-memory by the Python owner;
         * WGO never persists this raw stream as a log. */
        printf("WGO_KEY %s %s\n",
            keys[i].key_hex,
            keys[i].salt_hex[0] ? keys[i].salt_hex : "-");
    }
    printf("\nMatched %d/%d keys to known DBs\n", matched, key_count);

    /* Save JSON: { "rel/path.db": { "enc_key": "hex" }, ... }
     * Uses forward slashes (native macOS paths, valid JSON without escaping).
     */
    const char *out_path = "all_keys.json";
    FILE *fp = fopen(out_path, "w");
    if (fp) {
        fprintf(fp, "{\n");
        int first = 1;
        for (int i = 0; i < key_count; i++) {
            const char *db = NULL;
            for (int j = 0; j < g_db_count; j++) {
                if (strcmp(keys[i].salt_hex, g_db_salts[j]) == 0) {
                    db = g_db_names[j];
                    break;
                }
            }
            if (!db) continue;
            fprintf(fp, "%s  \"%s\": {\"enc_key\": \"%s\"}",
                first ? "" : ",\n", db, keys[i].key_hex);
            first = 0;
        }
        fprintf(fp, "\n}\n");
        fclose(fp);
        printf("Saved to %s\n", out_path);
    }

    return 0;
}
