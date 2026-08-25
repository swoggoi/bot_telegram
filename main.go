package main

import (
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
)

var (
	bot         *tgbotapi.BotAPI
	adminID     int64
	channelID   int64
	processed   = make(map[int64]bool)
	processedMu sync.Mutex
)

func main() {
	token := os.Getenv("BOT_TOKEN")
	if token == "" {
		log.Fatal("BOT_TOKEN не задан")
	}

	channelStr := os.Getenv("CHANNEL_ID")
	if channelStr == "" {
		log.Fatal("CHANNEL_ID не задан")
	}
	var err error
	channelID, err = strconv.ParseInt(channelStr, 10, 64)
	if err != nil {
		log.Fatalf("CHANNEL_ID невалидный: %s", channelStr)
	}

	adminStr := os.Getenv("ADMIN_ID")
	if adminStr != "" {
		adminID, _ = strconv.ParseInt(adminStr, 10, 64)
	}

	bot, err = tgbotapi.NewBotAPI(token)
	if err != nil {
		log.Fatalf("Ошибка создания бота: %v", err)
	}
	log.Printf("Бот: @%s (ID: %d)", bot.Self.UserName, bot.Self.ID)
	log.Printf("Канал: %d", channelID)
	if adminID != 0 {
		log.Printf("Админ: %d", adminID)
	}

	member, err := bot.GetChatMember(tgbotapi.GetChatMemberConfig{
		ChatConfigWithUser: tgbotapi.ChatConfigWithUser{
			ChatID: channelID,
			UserID: bot.Self.ID,
		},
	})
	if err != nil {
		log.Printf("Не удалось проверить права: %v", err)
	} else {
		log.Printf("Права бота: status=%s", member.Status)
	}

	bot.Request(tgbotapi.DeleteWebhookConfig{
		DropPendingUpdates: true,
	})

	u := tgbotapi.NewUpdate(0)
	u.Timeout = 60
	u.AllowedUpdates = []string{"message", "chat_join_request"}

	updates := bot.GetUpdatesChan(u)

	for update := range updates {
		if update.ChatJoinRequest != nil {
			go handleJoinRequest(*update.ChatJoinRequest)
		}
		if update.Message != nil {
			go handleMessage(*update.Message)
		}
	}
}

func handleJoinRequest(req tgbotapi.ChatJoinRequest) {
	if req.Chat.ID != channelID {
		log.Printf("Заявка в другой канал (chat_id=%d), ожидался %d. Пропуск.", req.Chat.ID, channelID)
		return
	}

	userID := req.From.ID
	username := req.From.UserName
	if username == "" {
		username = "bez_nika"
	}
	fullName := strings.TrimSpace(req.From.FirstName + " " + req.From.LastName)
	if fullName == "" {
		fullName = "Bez imeni"
	}

	processedMu.Lock()
	if processed[userID] {
		processedMu.Unlock()
		log.Printf("Заявка ID:%d уже обработана. Пропуск.", userID)
		return
	}
	processedMu.Unlock()

	log.Printf("Новая заявка: %s (ID: %d, @%s)", fullName, userID, username)

	cfg := tgbotapi.ApproveChatJoinRequestConfig{
		ChatConfig: tgbotapi.ChatConfig{ChatID: channelID},
		UserID:     userID,
	}
	if _, err := bot.Request(cfg); err != nil {
		log.Printf("Ошибка одобрения %s (ID: %d): %v", fullName, userID, err)
		return
	}

	processedMu.Lock()
	processed[userID] = true
	processedMu.Unlock()
	log.Printf("Заявка %s (ID: %d) ОДОБРЕНА.", fullName, userID)

	msg := tgbotapi.NewMessage(userID, "Привет! Рад видеть тебя в канале. Твоя заявка одобрена!")
	if _, err := bot.Send(msg); err != nil {
		log.Printf("Не удалось отправить ЛС ID:%d: %v", userID, err)
	}
}

func handleMessage(msg tgbotapi.Message) {
	if msg.From == nil {
		return
	}
	if adminID == 0 || msg.From.ID != adminID {
		return
	}

	text := msg.Text

	if text == "/status" {
		processedMu.Lock()
		count := len(processed)
		processedMu.Unlock()
		sendMsg(msg.Chat.ID, fmt.Sprintf(
			"Бот запущен и работает\n\nID канала: %d\nРежим: автоматическое одобрение заявок\nОбработано за сессию: %d",
			channelID, count,
		))
		return
	}

	if text == "/chat_id" {
		sendMsg(msg.Chat.ID, fmt.Sprintf(
			"Информация о чате:\nID чата: %d\nТип: %s",
			msg.Chat.ID, msg.Chat.Type,
		))
		return
	}

	if strings.HasPrefix(text, "/approve_pending") {
		parts := strings.Fields(text)
		if len(parts) < 2 {
			sendMsg(msg.Chat.ID, "Использование: /approve_pending user_id1 [user_id2 ...]\nПример: /approve_pending 123456789")
			return
		}

		approved := 0
		failed := 0
		for _, arg := range parts[1:] {
			uid, err := strconv.ParseInt(arg, 10, 64)
			if err != nil {
				sendMsg(msg.Chat.ID, fmt.Sprintf("'%s' не является валидным ID.", arg))
				continue
			}

			cfg := tgbotapi.ApproveChatJoinRequestConfig{
				ChatConfig: tgbotapi.ChatConfig{ChatID: channelID},
				UserID:     uid,
			}
			if _, err := bot.Request(cfg); err != nil {
				log.Printf("Ошибка одобрения ID:%d: %v", uid, err)
				failed++
				continue
			}

			processedMu.Lock()
			processed[uid] = true
			processedMu.Unlock()
			approved++
			log.Printf("Заявка ID:%d одобрена командой /approve_pending", uid)

			notif := tgbotapi.NewMessage(uid, "Привет! Рад видеть тебя в канале. Твоя заявка одобрена!")
			if _, err := bot.Send(notif); err != nil {
				log.Printf("Не удалось отправить ЛС ID:%d: %v", uid, err)
			}
		}

		sendMsg(msg.Chat.ID, fmt.Sprintf("Результат:\n  Одобрено: %d\n  Ошибок: %d", approved, failed))
	}
}

func sendMsg(chatID int64, text string) {
	msg := tgbotapi.NewMessage(chatID, text)
	if _, err := bot.Send(msg); err != nil {
		log.Printf("Ошибка отправки: %v", err)
	}
}
