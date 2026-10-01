-- One block of the blob per request at a random block-aligned offset, the
-- way the miniOSv arm reads it. Sizes come from the environment the instance
-- script sets; BENCH_CLOSE=1 dials a connection per request, which is that
-- arm's shape (it sends Connection: close on every block).
local block   = tonumber(os.getenv("BENCH_BLOCK_SIZE") or "134217728")
local size    = tonumber(os.getenv("BENCH_OBJECT_SIZE") or "10737418240")
local nblocks = math.floor(size / block)
local counter = 0

setup = function(thread)
   thread:set("id", counter)
   counter = counter + 1
end

init = function(args)
   math.randomseed(os.time() + (wrk.thread:get("id") or 0))
   if os.getenv("BENCH_CLOSE") == "1" then
      wrk.headers["Connection"] = "close"
   end
end

request = function()
   local a = math.random(0, nblocks - 1) * block
   wrk.headers["Range"] = string.format("bytes=%d-%d", a, a + block - 1)
   return wrk.format("GET", "/blob.bin")
end
