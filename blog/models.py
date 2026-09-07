from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models import Index, Q
from django.utils import timezone


class User(models.Model):
    username = models.CharField(max_length=64, unique=True)
    email = models.CharField(max_length=255, db_index=True)
    display_name = models.CharField(max_length=128)
    bio = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.username


class Tag(models.Model):
    name = models.CharField(max_length=64, unique=True)
    slug = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.name


class Post(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    title = models.CharField(max_length=255)
    body = models.TextField()
    is_published = models.BooleanField(default=True)
    view_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    tags = models.ManyToManyField(Tag, related_name="posts", blank=True)

    def __str__(self) -> str:
        return self.title

    class Meta:
        indexes = [
            # Partial index covering only published posts — smaller and faster than a
            # full composite index because every public list query filters is_published=True.
            Index(
                fields=["-created_at"],
                condition=Q(is_published=True),
                name="post_published_created_idx",
            ),
            # GIN index on a tsvector expression over title + body for full-text search.
            GinIndex(
                SearchVector("title", "body", config="english"),
                name="post_fts_gin",
            ),
        ]


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
