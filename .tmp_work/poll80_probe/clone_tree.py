#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POLL-80 剪枝：把 browser_data 完整克隆目录做成 APFS 写时复制（CoW）副本，
节省磁盘（多份副本共享数据块，直到被修改才占新空间）。"""
import ctypes, os, shutil, stat, sys

def clonefile(src, dst):
    libc = ctypes.CDLL("libc.dylib")
    r = libc.clonefile(os.fsencode(src), os.fsencode(dst), 0)
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), src)

def clone_tree(src, dst):
    """递归 clone：目录 mkdir，文件/链接 clonefile。遇错误回退普通复制。"""
    st = os.lstat(src)
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        # symlink: 复制链接
        os.symlink(os.readlink(src), dst)
        return
    elif stat.S_ISDIR(mode):
        os.mkdir(dst, st.st_mode & 0o777)
        for name in os.listdir(src):
            clone_tree(os.path.join(src, name), os.path.join(dst, name))
        shutil.copystat(src, dst, follow_symlinks=False)
    else:
        clonefile(src, dst)
        os.chmod(dst, st.st_mode & 0o777)

def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    os.makedirs(dst, exist_ok=False)
    # dst 已存在则清空
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        try:
            clone_tree(s, d)
        except OSError as e:
            # 单文件失败回退复制
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
    print(f"cloned {src} -> {dst}")

if __name__ == "__main__":
    main()