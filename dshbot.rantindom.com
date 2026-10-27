server {
    server_name dshbot.rantindom.com;

    location / {
        proxy_pass http://127.0.0.1:3704;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_request_buffering on;
    }

    listen 8443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/dshbot.rantindom.com/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/dshbot.rantindom.com/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot
}
server {
    if ($host = dshbot.rantindom.com) {
        return 301 https://$host$request_uri;
    } # managed by Certbot

    listen 80;
    server_name dshbot.rantindom.com;
    return 404; # managed by Certbot
}
