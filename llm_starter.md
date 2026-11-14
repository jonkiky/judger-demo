

Start server

export LLAMA_SERVER="$(find "$HOME/llama-cpp" -type f -name llama-server -print -quit)"
echo "$LLAMA_SERVER"


"$LLAMA_SERVER" \
  -hf Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M \
  --embedding \
  --pooling last \
  -ngl 0 \
  -c 2048 \
  --port 8080



"$LLAMA_SERVER"   -hf bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M   -ngl 0   -c 16384   --host 127.0.0.1   --port 8081


