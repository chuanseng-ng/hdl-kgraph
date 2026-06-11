// vendor-style filelist: comments, env vars, plusargs, nested lists
# hash comments are tolerated too
+incdir+$PREPROC_ROOT/include
+define+USE_FIFO+DEPTH=4
-f common.f
rtl/top.sv
include/defines.svh   // some flows list headers; spliced ones are skipped
-y lib_cells
-v prims.v
-sv
