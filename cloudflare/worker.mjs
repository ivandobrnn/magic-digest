const CHECK = "magic_digest_check";

export default {
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/telegram") {
      return new Response("Not found", { status: 404 });
    }
    if (!env.WEBHOOK_SECRET ||
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      const { callback_query: callback } = await request.json();
      const message = callback?.message;
      if (callback?.data !== CHECK || !message ||
          String(message.chat.id) !== env.TELEGRAM_CHAT_ID ||
          !Number.isSafeInteger(message.message_id)) {
        return new Response("OK");
      }

      const telegram = async (method, payload) => {
        const response = await fetch(
          `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload) },
        );
        return response.json();
      };
      const answer = (text) => telegram("answerCallbackQuery", {
        callback_query_id: callback.id, text,
      });
      const membership = await telegram("getChatMember", {
        chat_id: env.TELEGRAM_CHAT_ID, user_id: callback.from.id,
      });
      if (!membership.ok) {
        await answer("Не удалось проверить права. Попробуй ещё раз.");
        return new Response("OK");
      }
      if (!["creator", "administrator"].includes(membership.result.status)) {
        await answer("Проверку может запустить администратор канала.");
        return new Response("OK");
      }

      const dispatch = await fetch(
        "https://api.github.com/repos/ivandobrnn/magic-digest/actions/workflows/digest.yml/dispatches",
        {
          method: "POST",
          headers: {
            "Accept": "application/vnd.github+json",
            "Authorization": `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
            "Content-Type": "application/json",
            "User-Agent": "magic-digest-telegram",
          },
          body: JSON.stringify({
            ref: "main",
            inputs: { mode: "publish", button_message_id: String(message.message_id) },
          }),
        },
      );
      await answer(dispatch.ok ? "Проверка поставлена в очередь." : "GitHub отклонил запуск.");
      if (!dispatch.ok) console.warn("GitHub dispatch status", dispatch.status);
      return new Response("OK");
    } catch {
      console.warn("Could not process Telegram callback");
      return new Response("OK");
    }
  },
};
