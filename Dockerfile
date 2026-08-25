FROM golang:1.23-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bot .

FROM scratch
COPY --from=builder /bot /bot
ENTRYPOINT ["/bot"]
